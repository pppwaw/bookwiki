from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from bookwiki.agents import (
    CardAgent,
    ChapterAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptExtractAgent,
    ConceptReconcileAgent,
    QuizAgent,
    ReviewAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
)
from bookwiki.convert.common import source_id_from_stem
from bookwiki.convert.mineru_client import convert_document_to_md
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.indexer.sqlite_builder import build_sqlite_index
from bookwiki.integrator.markdown_renderers import normalize_mdx_math
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.split.chapter_splitter import parse_approved_structure
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_text

State = dict[str, Any]


def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _json_model(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)


def _agent_result_payload(agent_cls: type[Any], model: str, result: Any) -> dict[str, Any]:
    payload = _json_model(result)
    return {
        "_schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "_agent": agent_cls.__name__,
        "_model": model,
        "result": payload,
    }


def _agent_result(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result")
    return result if isinstance(result, dict) else data


def _read_all_markdown(paths: list[Path]) -> str:
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)


def _chapter_titles(approved_structure: str) -> list[tuple[str, str]]:
    return [
        (chapter.chapter_id, chapter.title)
        for chapter in parse_approved_structure(approved_structure)
    ]


def _cache_dir(cfg: BookConfig) -> Path:
    return cfg.cache_dir / "tasks"


def _stage_cache_hit(results: list[CacheResult]) -> bool:
    return bool(results) and all(item.cache_hit for item in results)


def _safe_file_stem(value: str, *, fallback_prefix: str = "item") -> str:
    normalized = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-.")
    if normalized:
        return normalized
    return f"{fallback_prefix}-{sha256_text(value)[:8]}"


def _unique_file_stem(value: str, used: set[str], *, fallback_prefix: str = "item") -> str:
    stem = _safe_file_stem(value, fallback_prefix=fallback_prefix)
    candidate = stem
    if candidate in used:
        digest = sha256_text(value)[:8]
        candidate = f"{stem}-{digest}"
        counter = 2
        while candidate in used:
            candidate = f"{stem}-{digest}-{counter}"
            counter += 1
    used.add(candidate)
    return candidate


def _clear_generated_files(directory: Path, pattern: str) -> None:
    for path in directory.glob(pattern):
        if path.is_file():
            path.unlink()


def _mdx_prop(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _citation_items(citations: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"ref_id": str(item.get("ref_id", "")), "quote": str(item.get("quote", ""))}
        for item in citations
    ]


def _quiz_items_for_mdx(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        rendered.append(
            {
                "id": str(item.get("id") or f"quiz-{index:03d}"),
                "question": str(item.get("question", "")),
                "choices": [str(choice) for choice in item.get("choices", [])],
                "answer": str(item.get("answer", "")),
                "explanation": str(item.get("explanation", "")),
                "citations": _citation_items(item.get("citations", [])),
            }
        )
    return rendered


def _card_items_for_mdx(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        rendered.append(
            {
                "id": str(item.get("id") or f"card-{index:03d}"),
                "front": str(item.get("front", "")),
                "back": str(item.get("back", "")),
                "citations": _citation_items(item.get("citations", [])),
            }
        )
    return rendered


def _frontmatter(data: dict[str, Any]) -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{body}\n---\n\n"


def _quiz_block_mdx(title: str, items: list[dict[str, Any]]) -> str:
    props = _mdx_prop(_quiz_items_for_mdx(items))
    return f"## {title or 'Quiz'}\n\n<QuizBlock items={{{props}}} />"


def _insert_quiz_blocks(body_md: str, quiz: dict[str, Any]) -> str:
    blocks = [block.strip() for block in re.split(r"\n{2,}", body_md.strip()) if block.strip()]
    items = [item for item in quiz.get("items", []) if isinstance(item, dict)]
    placements = _quiz_placements_for_render(quiz, len(items))
    if not items:
        return body_md.strip()
    if not placements:
        quiz_block = _quiz_block_mdx("Quiz", items)
        if len(blocks) < 2:
            return f"{body_md.strip()}\n\n{quiz_block}"
        insert_after = max(1, (len(blocks) + 1) // 2)
        merged = [*blocks[:insert_after], quiz_block, *blocks[insert_after:]]
        return "\n\n".join(merged)

    chunks_by_after: dict[int, list[str]] = {}
    used_indexes: set[int] = set()
    for placement in placements:
        indexes = [
            index
            for index in placement["item_indexes"]
            if 1 <= index <= len(items) and index not in used_indexes
        ]
        if not indexes:
            continue
        used_indexes.update(indexes)
        selected = [items[index - 1] for index in indexes]
        after = min(max(int(placement["after_block"]), 0), max(len(blocks) - 1, 0))
        chunks_by_after.setdefault(after, []).append(
            _quiz_block_mdx(str(placement.get("title") or "Quiz"), selected)
        )

    unassigned = [
        items[index - 1] for index in range(1, len(items) + 1) if index not in used_indexes
    ]
    if unassigned:
        after = max(chunks_by_after, default=max(0, (len(blocks) - 1) // 2))
        chunks_by_after.setdefault(after, []).append(_quiz_block_mdx("Quiz", unassigned))

    merged: list[str] = []
    for index, block in enumerate(blocks):
        merged.append(block)
        if index in chunks_by_after:
            merged.extend(chunks_by_after[index])
    if not blocks:
        merged.extend(chunks_by_after.get(0, []))
    return "\n\n".join(merged)


def _quiz_placements_for_render(quiz: dict[str, Any], item_count: int) -> list[dict[str, Any]]:
    placements: list[dict[str, Any]] = []
    for raw in quiz.get("placements", []):
        if not isinstance(raw, dict):
            continue
        item_indexes = []
        for value in raw.get("item_indexes", []):
            try:
                item_indexes.append(int(value))
            except (TypeError, ValueError):
                continue
        if not item_indexes and item_count:
            continue
        try:
            after_block = int(raw.get("after_block", 0))
        except (TypeError, ValueError):
            after_block = 0
        placements.append(
            {
                "after_block": after_block,
                "item_indexes": item_indexes,
                "title": str(raw.get("title") or "Quiz"),
            }
        )
    return placements


def _document_title(body: str, fallback: str) -> str:
    frontmatter = re.match(r"^---\n(.*?)\n---", body, flags=re.DOTALL)
    if frontmatter:
        for line in frontmatter.group(1).splitlines():
            if line.startswith("title:"):
                return line.split(":", 1)[1].strip().strip('"')
    return next(
        (line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")),
        fallback,
    )


def _load_alias_map(state: State, cfg: BookConfig) -> dict[str, str]:
    raw = state.get("alias_map", {})
    if isinstance(raw, dict):
        if raw:
            return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, str):
        data = read_json(cfg.book_dir / raw, default={})
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
    reconciled = state.get("reconciled_concepts")
    if isinstance(reconciled, str):
        data = read_json(cfg.book_dir / reconciled, default={})
        alias_map = data.get("alias_map", {}) if isinstance(data, dict) else {}
        if isinstance(alias_map, dict):
            return {str(key): str(value) for key, value in alias_map.items()}
    return {}


def _normalize_concept_links(
    markdown: str, alias_map: dict[str, str], concept_stems: dict[str, str]
) -> str:
    def replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        canonical = alias_map.get(label) or alias_map.get(_concept_key(label)) or label
        stem = concept_stems.get(canonical)
        if stem:
            return f"[{canonical}](../concepts/{stem})"
        return f"[[{canonical}]]"

    return re.sub(r"\[\[([^\]]+)\]\]", replace, markdown)


def _concept_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _suspicious_phrases(markdown: str) -> list[str]:
    phrases = ["ignore previous instructions", "system prompt", "developer message"]
    lower = markdown.lower()
    return [phrase for phrase in phrases if phrase in lower]


def _mdx_link_exists(base_dir: Path, target: str) -> bool:
    clean = target.split("#", 1)[0]
    if not clean:
        return True
    path = (base_dir / clean).resolve()
    candidates = [path]
    if path.suffix == "":
        candidates.extend([path.with_suffix(".mdx"), path / "index.mdx"])
    return any(candidate.exists() for candidate in candidates)


def _allowed_source_refs(state: State, cfg: BookConfig) -> set[str]:
    refs: set[str] = set()
    for rel_path in state.get("sources_md", []):
        path = cfg.book_dir / rel_path
        if path.exists():
            refs.update(re.findall(r"source_ref:\s*([^\s>]+)", path.read_text(encoding="utf-8")))
    if refs:
        return refs
    for paths in state.get("agent_results", {}).values():
        for rel_path in paths.values():
            refs.update(_iter_citation_refs(read_json(cfg.book_dir / rel_path)))
    return refs


def _iter_citation_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        if "ref_id" in value and "quote" in value:
            refs.append(str(value["ref_id"]))
        for item in value.values():
            refs.extend(_iter_citation_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_iter_citation_refs(item))
    return refs


def _render_check_report_md(report: CheckReport) -> str:
    lines = ["# Check Report", "", f"Status: `{report.status}`", ""]
    if not report.issues:
        lines.append("No issues.")
        return "\n".join(lines) + "\n"
    for issue in report.issues:
        lines.append(
            f"- `{issue.severity}` `{issue.code}` owner `{issue.owner_task_id}`: {issue.message}"
        )
    return "\n".join(lines) + "\n"


def _owner_output_payload(owner_task_id: str, state: State, cfg: BookConfig) -> dict[str, Any]:
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return {}
    return read_json(path)


def _owner_artifact_path(owner_task_id: str, state: State, cfg: BookConfig) -> Path | None:
    if owner_task_id.startswith("concept:"):
        for name, rel_path in state.get("concept_pages", {}).items():
            path = cfg.book_dir / rel_path
            payload = read_json(path, default={})
            owner = str(_agent_result(payload).get("owner_task_id") or f"concept:{name}")
            if owner == owner_task_id:
                return path
        return None
    chapter_id, _, kind = owner_task_id.partition(":")
    rel_path = state.get("agent_results", {}).get(chapter_id, {}).get(kind)
    return cfg.book_dir / rel_path if rel_path else None


def _artifact_owner_task_id(ch_id: str, kind: str, payload: dict[str, Any]) -> str:
    result = _agent_result(payload)
    owner = result.get("owner_task_id")
    return str(owner) if owner else f"{ch_id}:{kind}"


def _concept_contexts(
    item: dict[str, Any], state: State, cfg: BookConfig
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for ch_id in item.get("source_chapter_ids", []):
        chapter_id_text = str(ch_id)
        paths = state.get("agent_results", {}).get(chapter_id_text, {})
        chapter = (
            _agent_result(read_json(cfg.book_dir / paths["chapter"]))
            if paths.get("chapter")
            else {}
        )
        summary = (
            _agent_result(read_json(cfg.book_dir / paths["summary"]))
            if paths.get("summary")
            else {}
        )
        source_rel = state.get("chapter_sources", {}).get(chapter_id_text)
        source_md = ""
        if source_rel:
            source_path = cfg.book_dir / source_rel
            if source_path.exists():
                source_md = source_path.read_text(encoding="utf-8")
        contexts.append(
            {
                "chapter_id": chapter_id_text,
                "title": chapter.get("title", chapter_id_text),
                "body_md": chapter.get("body_md", ""),
                "summary_md": summary.get("summary_md", ""),
                "source_md": source_md,
                "citations": [
                    *_citation_items(chapter.get("citations", [])),
                    *_citation_items(summary.get("citations", [])),
                ],
            }
        )
    return contexts


def _apply_repair(
    owner_task_id: str, issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> None:
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return
    payload = read_json(path)
    result = payload.get("result", payload)
    codes = {str(issue.get("code")) for issue in issues}
    allowed_refs = _allowed_source_refs(state, cfg)
    if "UNKNOWN_SOURCE_REF" in codes and allowed_refs:
        _replace_invalid_citation_refs(result, allowed_refs, sorted(allowed_refs)[0])
    _, _, kind = owner_task_id.partition(":")
    if kind == "quiz" and "QUIZ_ANSWER_NOT_IN_CHOICES" in codes:
        for item in result.get("items", []):
            choices = [str(choice) for choice in item.get("choices", [])]
            if choices and str(item.get("answer", "")) not in choices:
                item["answer"] = choices[0]
    elif kind == "card" and "EMPTY_CARD_SIDE" in codes:
        for index, item in enumerate(result.get("items", []), start=1):
            if not str(item.get("front", "")).strip():
                item["front"] = f"Card {index}"
            if not str(item.get("back", "")).strip():
                item["back"] = "Review the source material for this card."
    write_json(path, payload)


def _replace_invalid_citation_refs(value: Any, allowed_refs: set[str], replacement: str) -> None:
    if isinstance(value, dict):
        if "ref_id" in value and "quote" in value and str(value["ref_id"]) not in allowed_refs:
            value["ref_id"] = replacement
            if not str(value.get("quote", "")).strip():
                value["quote"] = "source context"
        for item in value.values():
            _replace_invalid_citation_refs(item, allowed_refs, replacement)
    elif isinstance(value, list):
        for item in value:
            _replace_invalid_citation_refs(item, allowed_refs, replacement)


def convert_node(state: State, cfg: BookConfig) -> State:
    input_files = sorted(path for path in cfg.input_dir.iterdir() if path.is_file())
    if not input_files:
        msg = f"no input files found in {cfg.input_dir}"
        raise FileNotFoundError(msg)

    out_dir = ensure_dir(cfg.work_dir / "sources_md")
    outputs: list[str] = []
    for path in input_files:
        source_id = source_id_from_stem(path.stem)
        out_path = out_dir / f"{source_id}.md"
        suffix = path.suffix.lower()
        if suffix in {".pdf", ".pptx"}:
            body = convert_document_to_md(path, source_id=source_id)
        elif suffix in {".txt", ".md"}:
            body = convert_text_to_md(path, source_id=source_id)
        else:
            msg = f"unsupported source file type: {path.name}"
            raise ValueError(msg)
        write_text(out_path, body)
        outputs.append(_rel(out_path, cfg.book_dir))

    return {"sources_md": outputs}


async def structure_node(state: State, cfg: BookConfig) -> State:
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    results: list[CacheResult] = []
    summaries = []
    for path in source_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        result = await run_with_cache(
            SourceSummaryAgent,
            {"path": str(path), "sha256": sha256_text(text), "language": cfg.language},
            model=cfg.model_for("source_summary"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        results.append(result)
        summaries.append(_json_model(result.result))

    structure = await run_with_cache(
        StructureAgent,
        {"summaries": summaries, "strategy": "pedagogical", "language": cfg.language},
        model=cfg.model_for("structure"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    results.append(structure)

    out_dir = ensure_dir(cfg.work_dir / "structure")
    proposed_path = write_text(
        out_dir / "proposed-structure.yaml", structure.result.proposed_structure_yaml
    )
    approved_path = out_dir / "approved-structure.yaml"
    if not approved_path.exists():
        write_text(approved_path, structure.result.proposed_structure_yaml)
    write_text(
        out_dir / "structure-review.md",
        "# Structure Review\n\n"
        "Review `proposed-structure.yaml`, edit `approved-structure.yaml`, then run split.\n\n"
        f"Source summaries: {len(summaries)}\n",
    )

    return {
        "proposed_structure": _rel(proposed_path, cfg.book_dir),
        "approved_structure": _rel(approved_path, cfg.book_dir),
        "cache_hit": _stage_cache_hit(results),
    }


async def split_node(state: State, cfg: BookConfig) -> State:
    approved_path = cfg.book_dir / state.get(
        "approved_structure", "work/structure/approved-structure.yaml"
    )
    approved_structure = approved_path.read_text(encoding="utf-8")
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    split = await run_with_cache(
        ChapterSplitAgent,
        {
            "source_paths": [str(path) for path in source_paths],
            "source_hashes": [
                sha256_text(path.read_text(encoding="utf-8", errors="ignore"))
                for path in source_paths
            ],
            "approved_structure": approved_structure,
        },
        model=cfg.model_for("split"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )

    out_dir = ensure_dir(cfg.work_dir / "chapter_sources")
    _clear_chapter_source_dirs(out_dir)
    chapter_sources: dict[str, str] = {}
    titles = split.result.chapter_titles or dict(_chapter_titles(approved_structure))
    for ch_id, md in split.result.chapters.items():
        title = titles.get(ch_id, ch_id)
        chapter_dir = ensure_dir(out_dir / ch_id)
        path = write_text(
            chapter_dir / "source.md",
            md if md.startswith("#") else f"# {ch_id} {title}\n\n{md.strip()}\n",
        )
        chapter_sources[ch_id] = _rel(path, cfg.book_dir)
    alignment_path = write_json(
        out_dir / "_alignment.json",
        {
            "alignment": split.result.alignment,
            "coverage": split.result.coverage,
            "chapter_titles": titles,
        },
    )
    report_path = write_text(
        cfg.work_dir / "logs" / "chapter-split-report.md", split.result.report_md
    )

    return {
        "chapter_sources": chapter_sources,
        "chapter_titles": titles,
        "chapter_alignment": _rel(alignment_path, cfg.book_dir),
        "chapter_split_report": _rel(report_path, cfg.book_dir),
        "cache_hit": split.cache_hit,
    }


def _clear_chapter_source_dirs(out_dir: Path) -> None:
    for child in out_dir.iterdir():
        if child.is_dir() and re.fullmatch(r"(ch\d+|chapter-\d+|appendix)", child.name):
            shutil.rmtree(child)


async def generate_node(state: State, cfg: BookConfig) -> State:
    if not state.get("chapter_sources"):
        msg = "generate requires chapter_sources; run split before generate"
        raise ValueError(msg)
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    chapter_results: dict[str, dict[str, str]] = {}
    cache_results: list[CacheResult] = []
    titles = state.get("chapter_titles", {})

    for ch_id, rel_source in state.get("chapter_sources", {}).items():
        source_path = cfg.book_dir / rel_source
        source_md = source_path.read_text(encoding="utf-8")
        payload = {
            "chapter_id": ch_id,
            "title": titles.get(ch_id, ch_id),
            "source_md": source_md,
            "source_path": rel_source,
            "language": cfg.language,
            "quiz_per_chapter": cfg.quiz_per_chapter,
            "cards_per_chapter": cfg.cards_per_chapter,
        }
        chapter_model = cfg.model_for("chapter")
        summary_model = cfg.model_for("summary")
        quiz_model = cfg.model_for("quiz")
        card_model = cfg.model_for("card")
        chapter = await run_with_cache(
            ChapterAgent,
            payload,
            model=chapter_model,
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        chapter_payload = {
            **payload,
            "chapter_result": _json_model(chapter.result),
            "chapter_body_md": chapter.result.body_md,
        }
        summary, quiz, card = await asyncio.gather(
            run_with_cache(
                SummaryAgent,
                chapter_payload,
                model=summary_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
            run_with_cache(
                QuizAgent,
                chapter_payload,
                model=quiz_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
            run_with_cache(
                CardAgent,
                chapter_payload,
                model=card_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
        )
        cache_results.extend([chapter, summary, quiz, card])
        paths = {
            "chapter": write_json(
                result_dir / f"{ch_id}.chapter.json",
                _agent_result_payload(ChapterAgent, chapter_model, chapter.result),
            ),
            "summary": write_json(
                result_dir / f"{ch_id}.summary.json",
                _agent_result_payload(SummaryAgent, summary_model, summary.result),
            ),
            "quiz": write_json(
                result_dir / f"{ch_id}.quiz.json",
                _agent_result_payload(QuizAgent, quiz_model, quiz.result),
            ),
            "card": write_json(
                result_dir / f"{ch_id}.card.json",
                _agent_result_payload(CardAgent, card_model, card.result),
            ),
        }
        chapter_results[ch_id] = {name: _rel(path, cfg.book_dir) for name, path in paths.items()}

    return {"agent_results": chapter_results, "cache_hit": _stage_cache_hit(cache_results)}


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    agent_results = {
        str(ch_id): dict(paths)
        for ch_id, paths in state.get("agent_results", {}).items()
    }
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    cache_results: list[CacheResult] = []
    for ch_id, paths in state.get("agent_results", {}).items():
        if "concepts" in paths:
            extract = _agent_result(read_json(cfg.book_dir / paths["concepts"]))
            candidates.extend(extract.get("concepts", []))
            continue
        chapter = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        extract_result = await run_with_cache(
            ConceptExtractAgent,
            chapter,
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(extract_result)
        concepts_path = write_json(
            result_dir / f"{ch_id}.concepts.json",
            _agent_result_payload(
                ConceptExtractAgent, cfg.model_for("concept"), extract_result.result
            ),
        )
        agent_results.setdefault(str(ch_id), dict(paths))["concepts"] = _rel(
            concepts_path, cfg.book_dir
        )
        candidates.extend(
            item.model_dump(mode="json") for item in extract_result.result.concepts
        )
    result = await run_with_cache(
        ConceptReconcileAgent,
        candidates,
        model=cfg.model_for("concept"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    cache_results.append(result)
    out_dir = ensure_dir(cfg.work_dir / "concepts")
    reconciled = write_json(out_dir / "reconciled.json", _json_model(result.result))
    write_json(
        cfg.work_dir / "agent_results" / "concepts.reconciled.json",
        _json_model(result.result),
    )
    alias_map = write_json(out_dir / "alias_map.json", result.result.alias_map)
    return {
        "reconciled_concepts": _rel(reconciled, cfg.book_dir),
        "alias_map": _rel(alias_map, cfg.book_dir),
        "agent_results": agent_results,
        "cache_hit": _stage_cache_hit(cache_results),
    }


async def concept_pages_node(state: State, cfg: BookConfig) -> State:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    _clear_generated_files(out_dir, "*.json")
    outputs: dict[str, str] = {}
    cache_results: list[CacheResult] = []
    used_stems: set[str] = set()
    for item in data.get("concepts", []):
        concept_input = {
            **item,
            "chapter_contexts": _concept_contexts(item, state, cfg),
            "language": cfg.language,
        }
        result = await run_with_cache(
            ConceptAgent,
            concept_input,
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(result)
        safe_name = _unique_file_stem(
            result.result.name, used_stems, fallback_prefix="concept"
        )
        path = write_json(out_dir / f"{safe_name}.json", _json_model(result.result))
        outputs[result.result.name] = _rel(path, cfg.book_dir)
    return {"concept_pages": outputs, "cache_hit": _stage_cache_hit(cache_results)}


def integrate_node(state: State, cfg: BookConfig) -> State:
    content_dir = ensure_dir(cfg.content_dir)
    chapters_dir = ensure_dir(content_dir / "chapters")
    concepts_dir = ensure_dir(content_dir / "concepts")
    _clear_generated_files(chapters_dir, "*.mdx")
    _clear_generated_files(concepts_dir, "*.mdx")
    chapter_outputs: list[str] = []
    concept_backlinks: dict[str, list[dict[str, str]]] = {}
    alias_map = _load_alias_map(state, cfg)
    concept_stems = {
        str(name): Path(rel_path).stem
        for name, rel_path in state.get("concept_pages", {}).items()
    }

    for ch_id, paths in state.get("agent_results", {}).items():
        chapter = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        summary = _agent_result(read_json(cfg.book_dir / paths["summary"]))
        quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
        card = _agent_result(read_json(cfg.book_dir / paths["card"]))
        citations = chapter.get("citations", [])
        citation_md = "\n".join(f"- `{c['ref_id']}`: {c['quote']}" for c in citations)
        card_props = _mdx_prop(_card_items_for_mdx(card.get("items", [])))
        card_mdx = f"<AnkiDeck cards={{{card_props}}} />"
        concept_names = [str(name) for name in chapter.get("concepts", [])]
        for name in concept_names:
            concept_backlinks.setdefault(name, []).append(
                {"title": str(chapter["title"]), "href": f"../chapters/{ch_id}"}
            )
        path = write_text(
            chapters_dir / f"{ch_id}.mdx",
            (
                _frontmatter(
                    {
                        "chapter_id": ch_id,
                        "title": chapter["title"],
                        "type": "chapter",
                        "summary": summary["summary_md"],
                        "concepts": concept_names,
                    }
                )
                + _insert_quiz_blocks(
                    normalize_mdx_math(
                        _normalize_concept_links(
                            str(chapter["body_md"]), alias_map, concept_stems
                        )
                    ),
                    quiz,
                )
                + "\n\n"
                + f"## Sources\n\n{citation_md}\n\n"
                + f"## Anki Cards\n\n{card_mdx}\n"
            ),
        )
        chapter_outputs.append(_rel(path, cfg.book_dir))

    for name, rel_path in state.get("concept_pages", {}).items():
        concept = read_json(cfg.book_dir / rel_path)
        safe_name = Path(rel_path).stem or _safe_file_stem(name, fallback_prefix="concept")
        backlinks = concept_backlinks.get(str(name)) or concept_backlinks.get(
            str(concept["name"]), []
        )
        backlink_md = "\n".join(
            f"- [{item['title']}]({item['href']})" for item in backlinks
        )
        referenced_by = f"\n\n## Referenced By\n\n{backlink_md}\n" if backlink_md else ""
        write_text(
            concepts_dir / f"{safe_name}.mdx",
            _frontmatter({"title": concept["name"], "type": "concept"})
            + f"# {concept['name']}\n\n"
            + normalize_mdx_math(str(concept["body_md"]))
            + referenced_by,
        )

    index_path = write_text(
        content_dir / "index.mdx",
        _frontmatter({"title": cfg.title})
        + f"# {cfg.title}\n\n"
        + "\n".join(
            f"- [chapters/{Path(path).stem}](./chapters/{Path(path).stem})"
            for path in chapter_outputs
        )
        + "\n",
    )
    write_json(
        content_dir / "meta.json",
        {
            "title": cfg.title,
            "pages": [Path(path).with_suffix("").as_posix() for path in chapter_outputs],
        },
    )
    return {"content_ready": True, "content_index": _rel(index_path, cfg.book_dir)}


def check_node(state: State, cfg: BookConfig) -> State:
    issues: list[Issue] = []
    if not (cfg.content_dir / "index.mdx").exists():
        issues.append(
            Issue(
                severity="error",
                code="MISSING_CONTENT_INDEX",
                message="content/docs/index.mdx was not generated",
                owner_task_id="content:index",
            )
        )
    for path in (cfg.content_dir / "chapters").glob("*.mdx"):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_FRONTMATTER",
                    message=f"{path.name} has no YAML frontmatter",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        if "## Quiz" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_QUIZ",
                    message=f"{path.name} has no Quiz section",
                    owner_task_id=f"{path.stem}:quiz",
                )
            )
        if "## Anki Cards" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_ANKI",
                    message=f"{path.name} has no Anki Cards section",
                    owner_task_id=f"{path.stem}:card",
                )
            )
        if "## Sources" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_SOURCES",
                    message=f"{path.name} has no Sources section",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        for phrase in _suspicious_phrases(text):
            issues.append(
                Issue(
                    severity="warning",
                    code="SUSPICIOUS_INSTRUCTION",
                    message=f"{path.name} contains suspicious instruction text: {phrase}",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        for target in re.findall(r"\]\((?!https?://|mailto:|#)([^)]+)\)", text):
            if not _mdx_link_exists(path.parent, target):
                issues.append(
                    Issue(
                        severity="error",
                        code="BROKEN_LINK",
                        message=f"{path.name} links to missing target {target}",
                        owner_task_id=f"{path.stem}:chapter",
                    )
                )

    allowed_refs = _allowed_source_refs(state, cfg)
    for ch_id, paths in state.get("agent_results", {}).items():
        for kind, rel_path in paths.items():
            payload = read_json(cfg.book_dir / rel_path)
            for ref_id in _iter_citation_refs(payload):
                if allowed_refs and ref_id not in allowed_refs:
                    issues.append(
                        Issue(
                            severity="error",
                            code="UNKNOWN_SOURCE_REF",
                            message=f"{rel_path} cites unknown source_ref {ref_id}",
                            owner_task_id=_artifact_owner_task_id(ch_id, kind, payload),
                        )
                    )
        quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
        for index, item in enumerate(quiz.get("items", []), start=1):
            choices = [str(choice) for choice in item.get("choices", [])]
            answer = str(item.get("answer", ""))
            if answer not in choices:
                issues.append(
                    Issue(
                        severity="error",
                        code="QUIZ_ANSWER_NOT_IN_CHOICES",
                        message=f"{ch_id} quiz item {index} answer is not in choices",
                        owner_task_id=f"{ch_id}:quiz",
                    )
                )
        card = _agent_result(read_json(cfg.book_dir / paths["card"]))
        for index, item in enumerate(card.get("items", []), start=1):
            if not str(item.get("front", "")).strip() or not str(item.get("back", "")).strip():
                issues.append(
                    Issue(
                        severity="error",
                        code="EMPTY_CARD_SIDE",
                        message=f"{ch_id} card item {index} has an empty side",
                        owner_task_id=f"{ch_id}:card",
                    )
                )
    for name, rel_path in state.get("concept_pages", {}).items():
        payload = read_json(cfg.book_dir / rel_path)
        owner = str(_agent_result(payload).get("owner_task_id") or f"concept:{name}")
        for ref_id in _iter_citation_refs(payload):
            if allowed_refs and ref_id not in allowed_refs:
                issues.append(
                    Issue(
                        severity="error",
                        code="UNKNOWN_SOURCE_REF",
                        message=f"{rel_path} cites unknown source_ref {ref_id}",
                        owner_task_id=owner,
                    )
                )
    status = "needs_repair" if issues else "passed"
    report = CheckReport(status=status, issues=issues)
    logs_dir = ensure_dir(cfg.work_dir / "logs")
    report_path = write_json(logs_dir / "check-report.json", report.model_dump(mode="json"))
    write_json(cfg.work_dir / "check-report.json", report.model_dump(mode="json"))
    write_text(logs_dir / "check-report.md", _render_check_report_md(report))
    return {
        "check_report": _rel(report_path, cfg.book_dir),
        "repair_targets": report.repair_targets,
    }


async def repair_node(state: State, cfg: BookConfig) -> State:
    targets = state.get("repair_targets", [])
    if not targets:
        return {"repair_targets": []}
    max_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    rounds = dict(state.get("_repair_rounds", {}))
    out_dir = ensure_dir(cfg.work_dir / "repairs")
    outputs = []
    report = read_json(cfg.book_dir / state.get("check_report", "work/logs/check-report.json"))
    for target in targets:
        if int(rounds.get(target, 0)) >= max_rounds:
            continue
        rounds[target] = int(rounds.get(target, 0)) + 1
        target_issues = [
            issue for issue in report.get("issues", []) if issue.get("owner_task_id") == target
        ]
        result = await run_with_cache(
            ReviewAgent,
            {
                "owner_task_id": target,
                "issues": target_issues,
                "previous_output": _owner_output_payload(target, state, cfg),
            },
            model=cfg.model_for("review"),
            cache_dir=_cache_dir(cfg),
            force=True,
            runtime=cfg.llm_runtime,
        )
        _apply_repair(target, target_issues, state, cfg)
        path = write_json(out_dir / f"{target.replace(':', '-')}.json", _json_model(result.result))
        outputs.append(_rel(path, cfg.book_dir))
    return {"repairs": outputs, "repair_targets": [], "_repair_rounds": rounds}


def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    build_sqlite_index(cfg.content_dir, db_path)
    return {"sqlite": _rel(db_path, cfg.book_dir)}


NODE_FUNCTIONS = {
    "convert": convert_node,
    "structure": structure_node,
    "split": split_node,
    "generate": generate_node,
    "reconcile_concepts": reconcile_node,
    "concept_pages": concept_pages_node,
    "integrate": integrate_node,
    "check": check_node,
    "repair": repair_node,
    "index": index_node,
}
