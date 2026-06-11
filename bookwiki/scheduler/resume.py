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
        "chapter_alignment",
        "chapter_split_report",
    },
    "build_skeleton": {"skeleton"},
    "generate": {"agent_results", "generation_issues", "generated_figures"},
    "reconcile_concepts": {"reconciled_concepts", "alias_map"},
    "concept_pages": {"concept_pages"},
    "integrate": {"content_ready", "content_index"},
    "check": {"check_report", "repair_targets"},
    "repair": {"repairs", "repair_targets", "repair_exhausted"},
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
    if force_from:
        start_index = NODE_ORDER.index(force_from)
        for node_name in NODE_ORDER[start_index:]:
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
        chapter_sources, chapter_titles, alignment_path = existing_split_state(cfg)
        if chapter_sources:
            state["chapter_sources"] = chapter_sources
            if chapter_titles:
                state["chapter_titles"] = chapter_titles
            if alignment_path:
                state["chapter_alignment"] = alignment_path
    return state


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


def existing_split_state(cfg: BookConfig) -> tuple[dict[str, str], dict[str, str], str | None]:
    chapter_sources_dir = cfg.work_dir / "chapter_sources"
    if not chapter_sources_dir.exists():
        return {}, {}, None
    chapter_sources = {
        path.parent.name: path.relative_to(cfg.book_dir).as_posix()
        for path in sorted(chapter_sources_dir.glob("*/source.md"))
        if path.is_file()
    }
    alignment_path = chapter_sources_dir / "_alignment.json"
    alignment_rel = None
    chapter_titles: dict[str, str] = {}
    if alignment_path.exists():
        alignment_rel = alignment_path.relative_to(cfg.book_dir).as_posix()
        alignment = read_json(alignment_path, default={})
        raw_titles = alignment.get("chapter_titles", {})
        if isinstance(raw_titles, dict):
            chapter_titles = {str(key): str(value) for key, value in raw_titles.items()}
    return chapter_sources, chapter_titles, alignment_rel


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
