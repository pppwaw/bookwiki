from __future__ import annotations

import asyncio
import shutil
from html import unescape
from pathlib import Path
from typing import Any

from bookwiki.agents import (
    ChapterSplitAgent,
    SkeletonExtractAgent,
    SkeletonFoldAgent,
    SourceSummaryAgent,
    StructureAgent,
)
from bookwiki.chunking import chunk_by_heading
from bookwiki.concepts import brief_for as _brief_for
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.convert.common import (
    BOOK_FIGURE_TAG_RE,
    parse_book_figure_tag,
)
from bookwiki.pipeline._shared import (
    _LOG,
    APPROVED_STRUCTURE_MARKER,
    PENDING_STRUCTURE_MARKER,
    State,
    _agent_result_payload,
    _cache_dir,
    _display_chapter_title,
    _json_model,
    _rel,
    _replace_book_figure,
    _stage_cache_hit,
    log_progress,
)
from bookwiki.pipeline.structure_scan import audit_coverage, scan_source_refs
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.source import SourceSummaryResult
from bookwiki.skeleton.fold import Registry
from bookwiki.split.chapter_splitter import compute_slug_remap, parse_approved_structure
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_text


def _chapter_titles(approved_structure: str) -> list[tuple[str, str]]:
    return [
        (chapter.chapter_id, chapter.title)
        for chapter in parse_approved_structure(approved_structure)
    ]


def _chapter_topics(approved_structure: str) -> dict[str, list[str]]:
    try:
        chapters = parse_approved_structure(approved_structure)
    except ValueError:
        return {}
    return {chapter.chapter_id: list(chapter.topics) for chapter in chapters}


def _pending_approved_structure_text(proposed_structure: str) -> str:
    return (
        f"{PENDING_STRUCTURE_MARKER}\n"
        "# Review this file, edit chapters/topics/source_refs as needed, then replace\n"
        f"# the first marker with `{APPROVED_STRUCTURE_MARKER}` before running split.\n"
        f"{proposed_structure.rstrip()}\n"
    )


def _assert_structure_approved(approved_structure: str) -> None:
    if any(line.strip() == APPROVED_STRUCTURE_MARKER for line in approved_structure.splitlines()):
        return
    msg = (
        "approved-structure.yaml is not marked as reviewed. Review "
        "work/structure/proposed-structure.yaml, edit work/structure/approved-structure.yaml, "
        f"then add a line exactly `{APPROVED_STRUCTURE_MARKER}` before running split."
    )
    raise ValueError(msg)


def _caption_blocks_by_id(cfg: BookConfig) -> dict[str, dict[str, Any]]:
    """Collect every manifest block keyed by ``block_id`` across all source manifests on disk.

    Captions are written into ``work/source_refs/*.json`` by the ``caption`` stage (which no
    longer mutates ``sources_md``). Reading the manifests straight from disk keeps split
    independent of pipeline-state threading, so ``--from split`` works even when
    ``source_ref_manifests`` was not re-seeded into state.
    """
    blocks: dict[str, dict[str, Any]] = {}
    refs_dir = cfg.work_dir / "source_refs"
    if not refs_dir.exists():
        return blocks
    for manifest_path in sorted(refs_dir.glob("*.json")):
        manifest = read_json(manifest_path, default={})
        if not isinstance(manifest, dict):
            continue
        for page in manifest.get("pages", []):
            if not isinstance(page, dict):
                continue
            for block in page.get("blocks", []):
                if isinstance(block, dict) and block.get("block_id"):
                    blocks[str(block["block_id"])] = block
    return blocks


def _inject_book_figure_captions(markdown: str, blocks_by_id: dict[str, dict[str, Any]]) -> str:
    """Re-render each ``<BookFigure id=.../>`` whose manifest block carries a caption.

    This lands vision captions inline in the per-chapter source while leaving ``sources_md``
    byte-identical to convert output. Only the first occurrence of each ``id`` is rewritten,
    mirroring the previous ``caption`` stage behaviour (``_replace_book_figure`` uses
    ``count=1``).
    """
    if not blocks_by_id:
        return markdown
    seen: set[str] = set()
    for tag in BOOK_FIGURE_TAG_RE.findall(markdown):
        block_id = unescape(parse_book_figure_tag(tag).get("id", ""))
        if not block_id or block_id in seen:
            continue
        seen.add(block_id)
        block = blocks_by_id.get(block_id)
        if block is None or not str(block.get("caption") or "").strip():
            continue
        markdown, _ = _replace_book_figure(markdown, block)
    return markdown


def _merge_source_chunk_summaries(
    source_id: str, chunks: list[SourceSummaryResult]
) -> SourceSummaryResult:
    if not chunks:
        return SourceSummaryResult(
            source_id=source_id,
            summary_md=f"Summary for {source_id}: no chunks produced.",
            source_refs=[],
        )

    source_refs: list[str] = []
    headings: list[str] = []
    key_terms: list[str] = []
    summary_parts: list[str] = []
    detected_chapters = []
    concept_candidates = []
    exam_questions = []
    is_exam = False
    detected_chapter_id: str | None = None
    detected_title: str | None = None

    for chunk in chunks:
        _append_unique_strings(source_refs, chunk.source_refs)
        _append_unique_strings(headings, chunk.headings)
        _append_unique_strings(key_terms, chunk.key_terms)
        if chunk.summary_md:
            summary_parts.append(chunk.summary_md)
        if chunk.detected_chapter_id and detected_chapter_id is None:
            detected_chapter_id = chunk.detected_chapter_id
        if chunk.detected_title and detected_title is None:
            detected_title = chunk.detected_title
        detected_chapters.extend(chunk.detected_chapters)
        concept_candidates.extend(chunk.concept_candidates)
        is_exam = is_exam or chunk.is_exam
        exam_questions.extend(chunk.exam_questions)

    if len(detected_chapters) == 1:
        detected_chapters = [
            detected_chapters[0].model_copy(update={"source_refs": list(source_refs)})
        ]

    return SourceSummaryResult(
        source_id=source_id,
        summary_md="\n\n".join(summary_parts),
        source_refs=source_refs,
        detected_chapter_id=detected_chapter_id,
        detected_title=detected_title,
        headings=headings,
        key_terms=key_terms,
        detected_chapters=detected_chapters,
        concept_candidates=concept_candidates,
        is_exam=is_exam,
        exam_questions=exam_questions,
    )


def _append_unique_strings(target: list[str], values: list[str]) -> None:
    seen = set(target)
    for value in values:
        if value in seen:
            continue
        target.append(value)
        seen.add(value)


def _concept_candidates_by_ref(
    summaries: list[SourceSummaryResult],
) -> dict[str, list[dict[str, Any]]]:
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        for candidate in summary.concept_candidates:
            payload = candidate.model_dump(mode="json")
            refs = candidate.source_refs or summary.source_refs
            for ref in refs:
                bucket = by_ref.setdefault(ref, [])
                if payload not in bucket:
                    bucket.append(payload)
    return by_ref


async def structure_node(state: State, cfg: BookConfig) -> State:
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    _LOG.info("structure: source_files=%d", len(source_paths))
    results: list[CacheResult] = []
    summaries = []
    merged_summaries: list[SourceSummaryResult] = []
    book_notes = cfg.book_notes
    model = cfg.model_for("source_summary")
    all_refs: set[str] = set()
    covered_refs: set[str] = set()
    src_total = len(source_paths)
    for src_idx, path in enumerate(source_paths, 1):
        text = path.read_text(encoding="utf-8", errors="ignore")
        source_id = path.stem
        all_refs |= scan_source_refs(text)
        # Chunk the source so no single summary call can be silently truncated by
        # compact_input (the whole-book-in-one-call failure for >1M-token books). Each
        # chunk is bounded below the model's per-field budget by ``chunk_by_heading``.
        chunks = chunk_by_heading(text, model=model, stage="structure")
        spans = [(c.text, list(c.heading_path), list(c.source_refs)) for c in chunks] or [
            (text, [], sorted(all_refs))
        ]
        chunk_summaries: list[SourceSummaryResult] = []
        for span_text, heading_path, span_refs in spans:
            covered_refs.update(span_refs)
            result = await run_with_cache(
                SourceSummaryAgent,
                {
                    "span_text": span_text,
                    "source_id": source_id,
                    "path": str(path),
                    "heading_path": heading_path,
                    "sha256": sha256_text(span_text),
                    "language": cfg.language,
                    "book_notes": book_notes,
                },
                model=model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            )
            results.append(result)
            chunk_summaries.append(result.result)
        merged = _merge_source_chunk_summaries(source_id, chunk_summaries)
        merged_summaries.append(merged)
        summaries.append(_json_model(merged))
        log_progress(
            "structure",
            src_idx,
            src_total,
            "summarized source_id=%s chunks=%d",
            source_id,
            len(spans),
        )

    # Coverage audit: every ``<!-- source_ref -->`` in the sources must land in some chunk.
    # A miss means a chunking gap / truncation silently dropped part of the book — fail
    # loudly instead of proposing a structure that omits chapters.
    missing = audit_coverage(all_refs, covered_refs)
    if missing:
        msg = (
            "structure stage coverage audit failed: these source_refs were dropped "
            f"between chunks (chunking gap or truncation): {missing[:20]}"
        )
        raise ValueError(msg)

    _LOG.info(
        "structure: summaries done count=%d cache_hits=%d",
        len(summaries),
        sum(1 for r in results if r.cache_hit),
    )

    structure = await run_with_cache(
        StructureAgent,
        {
            "summaries": summaries,
            "strategy": "pedagogical",
            "language": cfg.language,
            "book_notes": book_notes,
        },
        model=cfg.model_for("structure"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    results.append(structure)

    out_dir = ensure_dir(cfg.work_dir / "structure")
    write_json(out_dir / "concept-candidates.json", _concept_candidates_by_ref(merged_summaries))
    write_json(out_dir / "exam-pool.json", _collect_exam_questions(merged_summaries))
    proposed_path = write_text(
        out_dir / "proposed-structure.yaml", structure.result.proposed_structure_yaml
    )
    approved_path = out_dir / "approved-structure.yaml"
    approved_needs_seed = (
        not approved_path.exists() or not approved_path.read_text(encoding="utf-8").strip()
    )
    if approved_needs_seed:
        write_text(
            approved_path,
            _pending_approved_structure_text(structure.result.proposed_structure_yaml),
        )
    write_text(
        out_dir / "structure-review.md",
        "# Structure Review\n\n"
        "Review `proposed-structure.yaml`, edit `approved-structure.yaml`, then replace "
        f"`{PENDING_STRUCTURE_MARKER}` with `{APPROVED_STRUCTURE_MARKER}` before running split.\n\n"
        f"Source summaries: {len(summaries)}\n",
    )
    cache_hits = sum(1 for r in results if r.cache_hit)
    _LOG.info(
        "structure: wrote proposed=%s approved_seed=%s cache_hits=%d/%d total=%d",
        _rel(proposed_path, cfg.book_dir),
        approved_needs_seed,
        cache_hits,
        len(results),
        len(results),
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
    _assert_structure_approved(approved_structure)
    _LOG.info(
        "split: approved_structure=%s cache_hit=%s",
        _rel(approved_path, cfg.book_dir),
        bool(approved_structure),
    )
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
            "book_notes": cfg.book_notes,
        },
        model=cfg.model_for("split"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )

    out_dir = ensure_dir(cfg.work_dir / "chapter_sources")
    chapter_sources: dict[str, str] = {}
    parse_titles = split.result.chapter_titles or dict(_chapter_titles(approved_structure))
    parse_order = list(split.result.chapter_order) or list(split.result.chapters.keys())
    registry_path = cfg.work_dir / "chapter_slugs.json"
    remap, registry = compute_slug_remap(
        parse_order,
        split.result.chapter_groups,
        parse_titles,
        split.result.chapter_source_refs,
        _load_slug_registry(registry_path),
    )
    write_json(registry_path, registry)

    def _rid(value: str) -> str:
        return remap.get(value, value)

    chapter_order = [_rid(ch_id) for ch_id in parse_order]
    titles = {_rid(k): v for k, v in parse_titles.items()}
    chapters_md = {_rid(k): v for k, v in split.result.chapters.items()}
    chapter_groups = {
        _rid(str(gid)): {
            **(info if isinstance(info, dict) else {}),
            "leaf_ids": [_rid(str(lid)) for lid in (info or {}).get("leaf_ids", []) or []],
        }
        for gid, info in split.result.chapter_groups.items()
    }
    alignment = [
        {**item, "chapter_id": _rid(str(item.get("chapter_id") or ""))}
        for item in split.result.alignment
    ]
    chapter_topics = {_rid(k): v for k, v in _chapter_topics(approved_structure).items()}

    _clear_chapter_source_dirs(out_dir, set(chapter_order))
    caption_blocks = _caption_blocks_by_id(cfg)
    _LOG.info(
        "split: chapters=%d chunked=%d groups=%d slug_remap=%d caption_blocks=%d",
        len(chapter_order),
        len(chapters_md),
        len(chapter_groups),
        len(remap),
        len(caption_blocks),
    )
    for ch_id in chapter_order:
        md = _inject_book_figure_captions(chapters_md[ch_id], caption_blocks)
        title = titles.get(ch_id, ch_id)
        chapter_dir = ensure_dir(out_dir / ch_id)
        path = write_text(
            chapter_dir / "source.md",
            md if md.startswith("#") else f"# {title}\n\n{md.strip()}\n",
        )
        chapter_sources[ch_id] = _rel(path, cfg.book_dir)
    alignment_path = write_json(
        out_dir / "_alignment.json",
        {
            "alignment": alignment,
            "coverage": split.result.coverage,
            "chapter_titles": titles,
            "chapter_groups": chapter_groups,
            "chapter_order": chapter_order,
        },
    )
    report_path = write_text(
        cfg.work_dir / "logs" / "chapter-split-report.md", split.result.report_md
    )
    _LOG.info(
        "split: done chapter_sources=%d cache_hit=%s report=%s",
        len(chapter_sources),
        split.cache_hit,
        _rel(report_path, cfg.book_dir),
    )

    return {
        "chapter_sources": chapter_sources,
        "chapter_titles": titles,
        "chapter_order": chapter_order,
        "chapter_topics": chapter_topics,
        "chapter_groups": chapter_groups,
        "chapter_alignment": _rel(alignment_path, cfg.book_dir),
        "chapter_split_report": _rel(report_path, cfg.book_dir),
        "cache_hit": split.cache_hit,
    }


def _load_slug_registry(path: Path) -> dict[str, str]:
    """Load the persisted ``fingerprint -> slug`` chapter slug registry (empty when absent)."""
    if not path.exists():
        return {}
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def _clear_chapter_source_dirs(out_dir: Path, keep_ids: set[str]) -> None:
    """Remove stale chapter source directories before writing the fresh split.

    Any subdirectory whose name is not in ``keep_ids`` is removed. This intentionally does NOT
    rely on a ``chapter-N`` naming pattern, so free-form / CJK chapter slugs from a previous run
    that are no longer present are still cleaned up (a leftover dir would otherwise be picked up
    by a stale resume).
    """
    for child in out_dir.iterdir():
        if child.is_dir() and child.name not in keep_ids:
            shutil.rmtree(child)


async def build_skeleton_node(state: State, cfg: BookConfig) -> State:
    """Produce the read-only book-wide skeleton consumed by ``generate``.

    Streams the build instead of shipping the whole book to one LLM call (which would
    overflow a >1M-token book's context window). Pass 1 extracts concept candidates from
    each chapter's source in parallel — the source is chunked first so no single call
    exceeds the model budget. Pass 2 folds those candidates chapter-by-chapter in order,
    letting the model merge cross-language synonyms against the compact running registry
    it sees in context. The resulting ``BookSkeleton`` is written to ``work/skeleton.json``.
    """
    if not state.get("chapter_sources"):
        msg = "build_skeleton requires chapter_sources; run split before build_skeleton"
        raise ValueError(msg)

    chapter_sources: dict[str, str] = state["chapter_sources"]
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})
    _LOG.info("build_skeleton: chapters=%d", len(chapter_sources))
    chapter_order = list(chapter_sources.keys())
    model = cfg.model_for("skeleton")
    cache_results: list[CacheResult] = []

    # -- Pass 1: per-chapter parallel candidate extraction (source chunked first) --
    skel_total = len(chapter_sources)
    semaphore = asyncio.Semaphore(cfg.chapter_concurrency)

    async def extract_chapter(
        idx: int, ch_id: str, rel_source: str
    ) -> tuple[str, str, list[str], list[CacheResult]]:
        async with semaphore:
            source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
            title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
            topics = list(topics_by_chapter.get(ch_id, []))
            results: list[CacheResult] = []
            for chunk in chunk_by_heading(source_md, model=model, stage="skeleton"):
                results.append(
                    await run_with_cache(
                        SkeletonExtractAgent,
                        {
                            "chapter_id": ch_id,
                            "title": title,
                            "topics": topics,
                            "source_md": chunk.text,
                            "source_refs": chunk.source_refs,
                            "language": cfg.language,
                            "book_notes": cfg.book_notes,
                        },
                        model=model,
                        cache_dir=_cache_dir(cfg),
                        runtime=cfg.llm_runtime,
                    )
                )
            log_progress(
                "build_skeleton",
                idx,
                skel_total,
                "extracted ch_id=%s chunks=%d",
                ch_id,
                len(results),
            )
            return ch_id, title, topics, results

    extraction = await asyncio.gather(
        *(
            extract_chapter(idx, ch_id, rel)
            for idx, (ch_id, rel) in enumerate(chapter_sources.items(), 1)
        )
    )

    candidates_by_chapter: dict[str, list[dict[str, Any]]] = {}
    titles_by_chapter: dict[str, str] = {}
    topics_resolved: dict[str, list[str]] = {}
    for ch_id, title, topics, results in extraction:
        cache_results.extend(results)
        titles_by_chapter[ch_id] = title
        topics_resolved[ch_id] = topics
        seen: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for result in results:
            for cand in result.result.candidates:
                key = _concept_key(cand.name)
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(cand.model_dump(mode="json"))
        candidates_by_chapter[ch_id] = candidates

    # -- Pass 2: serial fold (each call sees only candidates + the compact registry) --
    registry = Registry()
    for index, ch_id in enumerate(chapter_order):
        log_progress("build_skeleton", index + 1, skel_total, "fold ch_id=%s", ch_id)
        fold = await run_with_cache(
            SkeletonFoldAgent,
            {
                "chapter_id": ch_id,
                "chapter_title": titles_by_chapter.get(ch_id, ch_id),
                "chapter_order": index,
                "candidates": candidates_by_chapter.get(ch_id, []),
                "registry": registry.compact(),
                "language": cfg.language,
                "book_notes": cfg.book_notes,
            },
            model=model,
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(fold)
        registry.apply(fold.result.ops, current_chapter=ch_id)
        registry.record_uses(ch_id, fold.result.uses)

    chapter_briefs = {
        ch_id: _brief_for(titles_by_chapter.get(ch_id, ch_id), topics_resolved.get(ch_id, []))
        for ch_id in chapter_order
    }
    skeleton = registry.to_skeleton(chapter_briefs=chapter_briefs, chapter_order=chapter_order)
    out_path = write_json(
        cfg.work_dir / "skeleton.json",
        _agent_result_payload(SkeletonFoldAgent, model, skeleton),
    )
    glossary_len = len(skeleton.glossary)
    _LOG.info(
        "build_skeleton: wrote %s glossary=%d cache_hit=%s",
        _rel(out_path, cfg.book_dir),
        glossary_len,
        _stage_cache_hit(cache_results),
    )
    return {
        "skeleton": _rel(out_path, cfg.book_dir),
        "cache_hit": _stage_cache_hit(cache_results),
    }


def _collect_exam_questions(summaries: list[SourceSummaryResult]) -> dict[str, Any]:
    """Flatten the questions of every ``is_exam`` summary for later per-chapter distribution."""
    questions = [
        question.model_dump(mode="json")
        for summary in summaries
        if summary.is_exam
        for question in summary.exam_questions
    ]
    return {"questions": questions}


__all__ = [
    "_chapter_titles",
    "_chapter_topics",
    "_pending_approved_structure_text",
    "_assert_structure_approved",
    "_caption_blocks_by_id",
    "_inject_book_figure_captions",
    "_merge_source_chunk_summaries",
    "_append_unique_strings",
    "_concept_candidates_by_ref",
    "structure_node",
    "split_node",
    "_load_slug_registry",
    "_clear_chapter_source_dirs",
    "build_skeleton_node",
    "_collect_exam_questions",
]
