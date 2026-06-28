"""Pure resume / rerun (``--from`` + ``--force``) / config-hash helpers.

Extracted from the legacy ``BookGraph`` so the LangGraph runner has a single,
class-free implementation of the intricate state-reconstruction logic. These are
plain functions over ``BookConfig`` plus on-disk artifacts; they do not touch any
checkpoint store (the checkpointer is the runner's concern).

Note: the rerun-from-a-node operation is triggered by the **two** CLI flags
``--from <node>`` and ``--force`` together; there is no ``--force-from`` flag.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from typing import Any

from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.dry_run import summarize
from bookwiki.utils.files import read_json

NODE_ORDER: list[str] = [
    "convert",
    "caption",
    "structure",
    "split",
    "build_skeleton",
    "generate",
    "reconcile_concepts",
    "concept_pages",
    "integrate",
    "check",
    "repair",
    "index",
]

NODE_OUTPUT_KEYS: dict[str, set[str]] = {
    "convert": {"sources_md", "source_ref_manifests"},
    "caption": {"caption_results"},
    "structure": {"proposed_structure", "approved_structure"},
    "split": {
        "chapter_sources",
        "chapter_titles",
        "chapter_order",
        "chapter_groups",
        "chapter_alignment",
        "chapter_split_report",
    },
    "build_skeleton": {"skeleton"},
    "generate": {
        "agent_results",
        "generation_issues",
        "generated_figures",
        "generated_figures_index",
    },
    "reconcile_concepts": {"reconciled_concepts", "alias_map"},
    "concept_pages": {"concept_pages", "concept_generation_issues"},
    "integrate": {"content_ready", "content_index"},
    "check": {"check_report", "repair_targets"},
    "repair": {
        "repairs",
        "mdx_edited",
        "repair_artifact_changed",
        "repair_targets",
        "repair_exhausted",
        # Repair-loop round budget. Carried across the integrate->check->repair loop *within* a
        # run (its termination guarantee), but cleared on a forced rerun so `--from`/`--force`
        # hands the loop a fresh budget instead of reviving last run's exhausted counters.
        "_repair_rounds",
    },
    "index": {"sqlite"},
}


def config_hash(cfg: BookConfig) -> str:
    config = cfg.to_json()
    config["book_notes_hash"] = hashlib.sha256(cfg.book_notes.encode("utf-8")).hexdigest()
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def state_after_config_change(
    cfg: BookConfig, checkpoint_state: dict[str, Any], stop_after: str | None
) -> tuple[dict[str, Any], int]:
    sources_md = checkpoint_state.get("sources_md")
    if sources_md and stop_after != "convert":
        source_ref_manifests = checkpoint_state.get(
            "source_ref_manifests"
        ) or existing_source_ref_manifests(cfg)
        if not source_ref_manifests:
            print("resume: config changed; rerunning from convert")
            return {"book_id": cfg.book_id}, 0
        print("resume: config changed; rerunning from caption")
        return (
            {
                "book_id": checkpoint_state.get("book_id", cfg.book_id),
                "sources_md": sources_md,
                "source_ref_manifests": source_ref_manifests,
            },
            NODE_ORDER.index("caption"),
        )
    print("resume: config changed; rerunning from convert")
    return {"book_id": cfg.book_id}, 0


def state_for_force_from(cfg: BookConfig, checkpoint_state: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = dict(checkpoint_state) if checkpoint_state else {}
    state["book_id"] = str(state.get("book_id") or cfg.book_id)
    force_from = cfg.force_from
    target_chapters = cfg.target_chapter_ids
    target_concepts = cfg.target_concept_names
    if force_from:
        start_index = NODE_ORDER.index(force_from)
        for node_name in NODE_ORDER[start_index:]:
            if node_name == "generate" and target_chapters:
                continue
            if node_name == "concept_pages" and target_concepts:
                continue
            for key in NODE_OUTPUT_KEYS.get(node_name, set()):
                state.pop(key, None)
    start_index = NODE_ORDER.index(force_from) if force_from else 0
    if force_from and start_index >= NODE_ORDER.index("caption") and not state.get("sources_md"):
        sources = existing_sources_md(cfg)
        if not sources:
            msg = (
                f"--from {force_from} requires converted markdown in "
                f"{cfg.work_dir / 'sources_md'}; run convert first"
            )
            raise FileNotFoundError(msg)
        state["sources_md"] = sources
    if force_from == "caption" and not state.get("source_ref_manifests"):
        manifests = existing_source_ref_manifests(cfg)
        if not manifests:
            msg = (
                "--from caption requires source ref manifests in "
                f"{cfg.work_dir / 'source_refs'}; run convert first"
            )
            raise FileNotFoundError(msg)
        state["source_ref_manifests"] = manifests
    if (
        force_from
        and start_index >= NODE_ORDER.index("generate")
        and not state.get("chapter_sources")
    ):
        chapter_sources, chapter_titles, chapter_groups, alignment_path, chapter_order = (
            existing_split_state(cfg)
        )
        if chapter_sources:
            state["chapter_sources"] = chapter_sources
            if chapter_titles:
                state["chapter_titles"] = chapter_titles
            if chapter_groups:
                state["chapter_groups"] = chapter_groups
            if alignment_path:
                state["chapter_alignment"] = alignment_path
            if chapter_order:
                state["chapter_order"] = chapter_order
    if force_from == "generate" and target_chapters:
        if not state.get("agent_results"):
            restored = existing_agent_results(cfg)
            if restored:
                state["agent_results"] = restored
        if not state.get("generated_figures"):
            restored_figures = existing_generated_figures(cfg)
            if restored_figures:
                state["generated_figures"] = restored_figures
                state["generated_figures_index"] = "work/generated_figures.json"
    if force_from == "concept_pages" and target_concepts:
        if not state.get("concept_pages"):
            restored_concepts = existing_concept_pages(cfg)
            if restored_concepts:
                state["concept_pages"] = restored_concepts
    return state


def existing_agent_results(cfg: BookConfig) -> dict[str, dict[str, str]]:
    result_dir = cfg.work_dir / "agent_results"
    if not result_dir.exists():
        return {}
    outputs: dict[str, dict[str, str]] = {}
    for chapter_path in sorted(result_dir.glob("*.chapter.json")):
        ch_id = chapter_path.name.removesuffix(".chapter.json")
        paths = {
            kind: result_dir / f"{ch_id}.{kind}.json"
            for kind in ("chapter", "summary", "quiz", "card")
        }
        if not all(path.exists() for path in paths.values()):
            continue
        outputs[ch_id] = {
            kind: path.relative_to(cfg.book_dir).as_posix() for kind, path in paths.items()
        }
        concepts = result_dir / f"{ch_id}.concepts.json"
        if concepts.exists():
            outputs[ch_id]["concepts"] = concepts.relative_to(cfg.book_dir).as_posix()
    return outputs


def existing_concept_pages(cfg: BookConfig) -> dict[str, str]:
    concepts_dir = cfg.work_dir / "agent_results" / "concepts"
    if not concepts_dir.exists():
        return {}
    outputs: dict[str, str] = {}
    for path in sorted(concepts_dir.glob("*.json")):
        payload = read_json(path, default={})
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            result = payload if isinstance(payload, dict) else {}
        name = str(result.get("name") or result.get("canonical") or path.stem).strip()
        if name:
            outputs[name] = path.relative_to(cfg.book_dir).as_posix()
    return outputs


def existing_generated_figures(cfg: BookConfig) -> dict[str, dict[str, str]]:
    path = cfg.work_dir / "generated_figures.json"
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return {}
    restored: dict[str, dict[str, str]] = {}
    for ch_id, figures in data.items():
        if isinstance(figures, dict):
            restored[str(ch_id)] = {str(figure_id): str(tag) for figure_id, tag in figures.items()}
    return restored


def existing_sources_md(cfg: BookConfig) -> list[str]:
    sources_dir = cfg.work_dir / "sources_md"
    if not sources_dir.exists():
        return []
    return [
        path.relative_to(cfg.book_dir).as_posix()
        for path in sorted(sources_dir.glob("*.md"))
        if path.is_file()
    ]


def existing_source_ref_manifests(cfg: BookConfig) -> list[str]:
    refs_dir = cfg.work_dir / "source_refs"
    if not refs_dir.exists():
        return []
    return [
        path.relative_to(cfg.book_dir).as_posix()
        for path in sorted(refs_dir.glob("*.json"))
        if path.is_file()
    ]


def existing_split_state(
    cfg: BookConfig,
) -> tuple[dict[str, str], dict[str, str], dict[str, Any], str | None, list[str]]:
    chapter_sources_dir = cfg.work_dir / "chapter_sources"
    if not chapter_sources_dir.exists():
        return {}, {}, {}, None, []
    on_disk = {
        path.parent.name: path.relative_to(cfg.book_dir).as_posix()
        for path in sorted(chapter_sources_dir.glob("*/source.md"))
        if path.is_file()
    }
    alignment_path = chapter_sources_dir / "_alignment.json"
    alignment_rel = None
    chapter_titles: dict[str, str] = {}
    chapter_groups: dict[str, Any] = {}
    persisted_order: list[str] = []
    if alignment_path.exists():
        alignment_rel = alignment_path.relative_to(cfg.book_dir).as_posix()
        alignment = read_json(alignment_path, default={})
        raw_titles = alignment.get("chapter_titles", {})
        if isinstance(raw_titles, dict):
            chapter_titles = {str(key): str(value) for key, value in raw_titles.items()}
        raw_groups = alignment.get("chapter_groups", {})
        if isinstance(raw_groups, dict):
            chapter_groups = raw_groups
        raw_order = alignment.get("chapter_order")
        if isinstance(raw_order, list):
            persisted_order = [str(cid) for cid in raw_order if str(cid)]
    if not on_disk:
        return {}, chapter_titles, chapter_groups, alignment_rel, []
    # Authoritative reading order: the persisted ``chapter_order``; for a legacy ``_alignment.json``
    # written before this field existed, fall back to the ``chapter_titles`` key order (also YAML
    # order, since JSON preserves insertion order). Never fall back to lexicographic glob order,
    # which silently corrupts the reading order once chapter ids are free-form slugs.
    order = persisted_order or [cid for cid in chapter_titles if cid in on_disk]
    if not order:
        msg = (
            f"cannot determine chapter reading order from {alignment_path} "
            "(missing chapter_order); rerun from split"
        )
        raise ValueError(msg)
    ordered_ids = [cid for cid in order if cid in on_disk]
    extra = sorted(set(on_disk) - set(ordered_ids))
    if extra:
        msg = (
            f"chapter_sources has directories absent from chapter_order {extra}; "
            "stale split state — rerun from split"
        )
        raise ValueError(msg)
    chapter_sources = {cid: on_disk[cid] for cid in ordered_ids}
    return chapter_sources, chapter_titles, chapter_groups, alignment_rel, ordered_ids


def clear_for_force(cfg: BookConfig) -> None:
    tasks = cfg.cache_dir / "tasks"
    if tasks.exists():
        shutil.rmtree(tasks)


def draw_mermaid() -> str:
    lines = ["graph TD", "    START --> convert"]
    for left, right in zip(NODE_ORDER, NODE_ORDER[1:], strict=False):
        if left == "check":
            lines.append("    check -->|issues| repair")
            lines.append("    check -->|clean| index")
        elif left == "repair":
            lines.append("    repair --> integrate")
        elif right != "repair":
            lines.append(f"    {left} --> {right}")
    lines.append("    index --> END")
    return "\n".join(lines)


def dry_run_report(cfg: BookConfig, chapter_count: int = 2) -> str:
    estimate = summarize(NODE_ORDER, chapter_count=chapter_count)
    return (
        f"{draw_mermaid()}\n\n"
        f"Estimated tokens: {estimate.tokens}\n"
        f"Estimated cost CNY: {estimate.cost_cny:.6f}\n"
        "Critical path: convert -> caption -> structure -> split -> "
        "generate -> check -> index\n"
    )
