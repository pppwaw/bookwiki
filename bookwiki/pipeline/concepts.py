from __future__ import annotations

import asyncio
from typing import Any

from bookwiki.agents import (
    ConceptAgent,
    ConceptContentRewriteAgent,
    ConceptExtractAgent,
    ConceptMdxEditRepairAgent,
    ConceptReconcileAgent,
)
from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.generate.sections import _body_too_short
from bookwiki.generate.validate_artifact import ArtifactIssue, validate_artifact
from bookwiki.pipeline._shared import (
    _LOG,
    State,
    _agent_result,
    _agent_result_payload,
    _cache_dir,
    _citation_items,
    _clear_generated_files,
    _display_chapter_title,
    _fanout_semaphores,
    _json_model,
    _load_skeleton,
    _rel,
    _safe_file_stem,
    _stage_cache_hit,
    log_progress,
)
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.concept import ConceptReconciledItem, ConceptReconcileResult, ConceptResult
from bookwiki.schemas.report import Issue
from bookwiki.utils.files import ensure_dir, read_json, write_json
from bookwiki.utils.hashing import sha256_text


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


def _concept_contexts(item: dict[str, Any], state: State, cfg: BookConfig) -> list[dict[str, Any]]:
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
                "title": _display_chapter_title(
                    chapter_id_text, str(chapter.get("title", chapter_id_text))
                ),
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


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    agent_results = {
        str(ch_id): dict(paths) for ch_id, paths in state.get("agent_results", {}).items()
    }
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    cache_results: list[CacheResult] = []
    chapters_in = list(state.get("agent_results", {}).items())
    reused_concepts = sum(1 for _ch, p in chapters_in if "concepts" in p)
    extracted_concepts = 0
    _LOG.info(
        "reconcile: chapters=%d with_cached_concepts=%d",
        len(chapters_in),
        reused_concepts,
    )
    rec_total = len(chapters_in)
    for rec_idx, (ch_id, paths) in enumerate(chapters_in, 1):
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
        candidates.extend(item.model_dump(mode="json") for item in extract_result.result.concepts)
        extracted_concepts += 1
        log_progress(
            "reconcile",
            rec_idx,
            rec_total,
            "extracted ch_id=%s concepts=%d",
            ch_id,
            len(extract_result.result.concepts),
        )

    skeleton = _load_skeleton(state, cfg)
    if skeleton is not None:
        _LOG.info(
            "reconcile: using skeleton merge candidates=%d extracted_chapters=%d",
            len(candidates),
            extracted_concepts,
        )
        reconciled_model = _merge_candidates_with_skeleton(skeleton, candidates)
    else:
        _LOG.info(
            "reconcile: skeleton absent, falling back to LLM reconcile candidates=%d",
            len(candidates),
        )
        reconcile = await run_with_cache(
            ConceptReconcileAgent,
            {
                "candidates": candidates,
                "language": cfg.language,
                "book_notes": cfg.book_notes,
            },
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(reconcile)
        reconciled_model = reconcile.result

    out_dir = ensure_dir(cfg.work_dir / "concepts")
    reconciled = write_json(out_dir / "reconciled.json", _json_model(reconciled_model))
    write_json(
        cfg.work_dir / "agent_results" / "concepts.reconciled.json",
        _json_model(reconciled_model),
    )
    alias_map = write_json(out_dir / "alias_map.json", reconciled_model.alias_map)
    _LOG.info(
        "reconcile: done concepts=%d alias_map=%d cache_hit=%s",
        len(reconciled_model.concepts),
        len(reconciled_model.alias_map),
        _stage_cache_hit(cache_results),
    )
    return {
        "reconciled_concepts": _rel(reconciled, cfg.book_dir),
        "alias_map": _rel(alias_map, cfg.book_dir),
        "agent_results": agent_results,
        "cache_hit": _stage_cache_hit(cache_results),
    }


def _merge_candidates_with_skeleton(
    skeleton: dict[str, Any], candidates: list[dict[str, Any]]
) -> ConceptReconcileResult:
    """Use the skeleton glossary as the pre-merged base and add any new candidates.

    Skeleton's glossary is already LLM-canonicalised at the ``build_skeleton``
    stage, so this step is purely deterministic: each candidate's normalised
    name is looked up in the skeleton's ``alias_map``; matches attach
    ``source_chapter_id`` to the existing canonical entry; non-matches are
    accumulated as new canonical concepts (rare — these are concepts a
    SectionAgent invented beyond the skeleton).
    """
    by_key: dict[str, ConceptReconciledItem] = {}
    alias_to_key: dict[str, str] = {}

    for entry in skeleton.get("glossary", []) or []:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical") or "").strip()
        if not canonical:
            continue
        first_chapter = str(entry.get("first_chapter_id") or "").strip()
        aliases = [str(a) for a in entry.get("aliases", []) if str(a).strip()]
        key = _concept_key(canonical)
        item = ConceptReconciledItem(
            canonical=canonical,
            aliases=aliases,
            source_chapter_ids=[first_chapter] if first_chapter else [],
        )
        by_key[key] = item
        alias_to_key[key] = key
        for alias in aliases:
            alias_to_key[_concept_key(alias)] = key

    skeleton_alias_map = dict(skeleton.get("alias_map", {}) or {})
    for variant, canonical in skeleton_alias_map.items():
        key = _concept_key(canonical)
        if key in by_key:
            alias_to_key[_concept_key(variant)] = key

    for cand in candidates:
        canonical = str(cand.get("name") or "").strip()
        if not canonical:
            continue
        aliases = [str(a) for a in cand.get("aliases", []) if str(a).strip()]
        chapter_id = str(cand.get("source_chapter_id") or "").strip()
        if not chapter_id:
            msg = f"concept candidate {canonical!r} is missing required 'source_chapter_id'"
            raise ValueError(msg)
        names = [canonical, *aliases]
        matched_key = next(
            (alias_to_key[_concept_key(n)] for n in names if _concept_key(n) in alias_to_key),
            None,
        )
        key = matched_key or _concept_key(canonical)
        existing = by_key.get(key)
        if existing is None:
            existing = ConceptReconciledItem(
                canonical=canonical,
                aliases=[],
                source_chapter_ids=[chapter_id],
            )
            by_key[key] = existing
        elif chapter_id and chapter_id not in existing.source_chapter_ids:
            existing.source_chapter_ids.append(chapter_id)
        for name in names:
            normalized = _concept_key(name)
            alias_to_key[normalized] = key
            if name != existing.canonical and name not in existing.aliases:
                existing.aliases.append(name)

    alias_map: dict[str, str] = dict(skeleton_alias_map)
    for item in by_key.values():
        alias_map[item.canonical] = item.canonical
        alias_map[_concept_key(item.canonical)] = item.canonical
        for alias in item.aliases:
            alias_map[alias] = item.canonical
            alias_map[_concept_key(alias)] = item.canonical

    return ConceptReconcileResult(concepts=list(by_key.values()), alias_map=alias_map)


async def concept_pages_node(state: State, cfg: BookConfig) -> State:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    _clear_generated_files(out_dir, "*.json")
    outputs: dict[str, str] = {}
    cache_results: list[CacheResult] = []
    concept_generation_issues: list[dict[str, Any]] = []
    used_stems: set[str] = set()
    glossary_names = [
        str(c.get("canonical")) for c in data.get("concepts", []) if c.get("canonical")
    ]
    concepts = list(data.get("concepts", []))
    _LOG.info("concept_pages: concepts=%d", len(concepts))
    for index, item in enumerate(concepts, start=1):
        chapter_contexts = _concept_contexts(item, state, cfg)
        concept_input = {
            **item,
            "chapter_contexts": chapter_contexts,
            "glossary": glossary_names,
            "language": cfg.language,
            "book_notes": cfg.book_notes,
        }
        result = await run_with_cache(
            ConceptAgent,
            concept_input,
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(result)
        concept, inline_cache, inline_issue = await _validate_concept_artifact_inline(
            cfg=cfg,
            concept=result.result,
            chapter_contexts=chapter_contexts,
        )
        cache_results.extend(inline_cache)
        if inline_issue is not None:
            concept_generation_issues.append(inline_issue.model_dump(mode="json"))
        safe_name = _unique_file_stem(concept.name, used_stems, fallback_prefix="concept")
        path = write_json(out_dir / f"{safe_name}.json", _json_model(concept))
        outputs[concept.name] = _rel(path, cfg.book_dir)
        log_progress(
            "concept_pages",
            index,
            len(concepts),
            "name=%s cache_hit=%s issue=%s",
            concept.name,
            result.cache_hit,
            bool(inline_issue),
        )
    _LOG.info(
        "concept_pages: done wrote=%d issues=%d cache_hit=%s",
        len(outputs),
        len(concept_generation_issues),
        _stage_cache_hit(cache_results),
    )
    return {
        "concept_pages": outputs,
        "concept_generation_issues": concept_generation_issues,
        "cache_hit": _stage_cache_hit(cache_results),
    }


def prepare_concept_pages_fanout_node(state: State, cfg: BookConfig) -> State:
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    targets = cfg.target_concept_names
    if targets:
        data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
        available = {_concept_item_name(item) for item in data.get("concepts", [])}
        missing = sorted(targets - available)
        if missing:
            msg = f"concept_pages target concept(s) not found in reconciled concepts: {missing}"
            raise ValueError(msg)
        _LOG.info(
            "prepare_concept_pages: targets=%d available=%d",
            len(targets),
            len(available),
        )
    else:
        _clear_generated_files(out_dir, "*.json")
        data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
        _LOG.info(
            "prepare_concept_pages: clearing outputs, available=%d",
            len(data.get("concepts", [])),
        )
    return {"_concept_page_parts": None, "cache_hit": False}


def concept_page_fanout_specs(state: State, cfg: BookConfig) -> list[State]:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    glossary_names = [
        str(c.get("canonical")) for c in data.get("concepts", []) if c.get("canonical")
    ]
    used_stems: set[str] = set()
    specs: list[State] = []
    targets = cfg.target_concept_names
    for order, item in enumerate(data.get("concepts", [])):
        name = _concept_item_name(item)
        safe_name = _unique_file_stem(name, used_stems, fallback_prefix="concept")
        if targets and name not in targets:
            continue
        specs.append(
            {
                "agent_results": state.get("agent_results", {}),
                "chapter_sources": state.get("chapter_sources", {}),
                "reconciled_concepts": state.get("reconciled_concepts"),
                "_fanout_concept_order": order,
                "_fanout_concept_item": item,
                "_fanout_concept_glossary": glossary_names,
                "_fanout_concept_stem": safe_name,
            }
        )
    total = len(specs)
    for idx, spec in enumerate(specs, 1):
        spec["_fanout_index"] = idx
        spec["_fanout_total"] = total
    return specs


def _concept_item_name(item: dict[str, Any]) -> str:
    return str(item.get("canonical") or item.get("name") or "concept").strip()


async def concept_page_fanout_node(state: State, cfg: BookConfig) -> State:
    item = dict(state["_fanout_concept_item"])
    order = int(state["_fanout_concept_order"])
    idx = int(state.get("_fanout_index", 0))
    total = int(state.get("_fanout_total", 0))
    name = str(item.get("canonical") or item.get("name") or f"concept-{order}")
    semaphore = _fanout_semaphores.setdefault(
        (id(cfg), "concept"), asyncio.Semaphore(cfg.chapter_concurrency)
    )
    async with semaphore:
        log_progress("concept_pages", idx, total, "start name=%s", name)
        try:
            part = await _run_concept_page_unit(state, cfg, item, order)
        except Exception:
            # Propagate so LangGraph re-runs only this concept on ``--resume`` (see the
            # generate worker for the full rationale).
            _LOG.exception("concept page failed for %s", name)
            raise
    log_progress(
        "concept_pages",
        idx,
        total,
        "done name=%s cache_hit=%s issue=%s",
        name,
        part.get("cache_hit"),
        bool(part.get("concept_generation_issues")),
    )
    return {"_concept_page_parts": {part["name"]: part}}


async def _run_concept_page_unit(
    state: State, cfg: BookConfig, item: dict[str, Any], order: int
) -> dict[str, Any]:
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    glossary_names = list(state.get("_fanout_concept_glossary", []))
    chapter_contexts = _concept_contexts(item, state, cfg)
    concept_input = {
        **item,
        "chapter_contexts": chapter_contexts,
        "glossary": glossary_names,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
    }
    cache_results: list[CacheResult] = []
    result = await run_with_cache(
        ConceptAgent,
        concept_input,
        model=cfg.model_for("concept"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    cache_results.append(result)
    concept, inline_cache, inline_issue = await _validate_concept_artifact_inline(
        cfg=cfg,
        concept=result.result,
        chapter_contexts=chapter_contexts,
    )
    cache_results.extend(inline_cache)
    safe_name = str(state["_fanout_concept_stem"])
    path = write_json(out_dir / f"{safe_name}.json", _json_model(concept))
    issues = [inline_issue.model_dump(mode="json")] if inline_issue is not None else []
    return {
        "name": concept.name,
        "order": order,
        "path": _rel(path, cfg.book_dir),
        "concept_generation_issues": issues,
        "cache_hit": _stage_cache_hit(cache_results),
    }


def collect_concept_pages_fanout_node(state: State, cfg: BookConfig) -> State:
    parts = state.get("_concept_page_parts") or {}
    targets = cfg.target_concept_names
    # Carry prior pages only on a targeted run (see the generate collect). A full run
    # regenerates every reconciled concept, so seeding from ``state`` would resurrect
    # stale entries whose files ``prepare_concept_pages`` already cleared.
    outputs: dict[str, str] = {
        str(name): str(path)
        for name, path in state.get("concept_pages", {}).items()
        if targets and str(name) not in targets
    }
    concept_generation_issues: list[dict[str, Any]] = [
        dict(issue)
        for issue in state.get("concept_generation_issues", [])
        if targets and _issue_concept_name(issue) not in targets
    ]
    cache_hits: list[bool] = []

    # A failed concept worker raises (aborting the super-step) instead of returning an
    # error part, so every part reaching collect is a success. See the generate collect.
    ordered = sorted(parts.values(), key=lambda part: int(part.get("order", 0)))
    for part in ordered:
        name = str(part.get("name") or "concept")
        if "path" not in part:  # a legacy swallowed-error part — heal via resume rewind
            raise RuntimeError(f"concept_pages produced no fanout result for: {name}")
        outputs[name] = str(part["path"])
        concept_generation_issues.extend(part.get("concept_generation_issues", []))
        cache_hits.append(bool(part.get("cache_hit", False)))

    _LOG.info(
        "collect_concept_pages: ok=%d issues=%d cache_hit=%s",
        len(outputs),
        len(concept_generation_issues),
        bool(cache_hits) and all(cache_hits),
    )
    return {
        "concept_pages": outputs,
        "concept_generation_issues": concept_generation_issues,
        "cache_hit": bool(cache_hits) and all(cache_hits),
    }


def _issue_concept_name(issue: dict[str, Any]) -> str | None:
    owner = str(issue.get("owner_task_id") or "")
    if not owner.startswith("concept:"):
        return None
    return owner.split(":", 1)[1]


async def _validate_concept_artifact_inline(
    *,
    cfg: BookConfig,
    concept: ConceptResult,
    chapter_contexts: list[dict[str, Any]],
) -> tuple[ConceptResult, list[CacheResult], Issue | None]:
    cache_results: list[CacheResult] = []
    allowed_refs = _allowed_refs_from_concept_contexts(chapter_contexts)
    max_mdx_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    max_quality_rounds = int(cfg.generation.get("maxQualityRounds", 1) or 1)
    mdx_rounds = 0
    quality_rounds = 0
    best_concept = concept
    best_issues: list[ArtifactIssue] | None = None

    while True:
        issues = await validate_artifact(
            body_md=concept.body_md,
            kind="concept",
            allowed_refs=allowed_refs,
            cfg=cfg,
        )
        if not issues:
            return concept, cache_results, None
        if best_issues is None or len(issues) < len(best_issues):
            best_concept = concept
            best_issues = issues

        mdx_issues = [issue for issue in issues if issue.kind == "mdx"]
        quality_issues = [issue for issue in issues if issue.kind == "quality"]
        if mdx_issues:
            if mdx_rounds >= max_mdx_rounds:
                break
            mdx_rounds += 1
            repaired = await run_with_cache(
                ConceptMdxEditRepairAgent,
                {
                    **concept.model_dump(mode="json"),
                    "mdx_errors": [issue.message for issue in mdx_issues],
                    "language": cfg.language,
                    "book_notes": cfg.book_notes,
                    "allowed_source_refs": sorted(allowed_refs),
                },
                model=cfg.model_for("mdx_repair"),
                cache_dir=_cache_dir(cfg),
                force=True,
                runtime=cfg.llm_runtime,
            )
            cache_results.append(repaired)
            candidate = repaired.result
            if _body_too_short(candidate.body_md, concept.body_md):
                _LOG.warning(
                    "discarding concept MDX repair for %s: body shrank from %d to %d chars",
                    concept.name,
                    len(concept.body_md),
                    len(candidate.body_md),
                )
                continue
            concept = candidate
            continue
        if quality_issues:
            if quality_rounds >= max_quality_rounds:
                break
            quality_rounds += 1
            rewritten = await run_with_cache(
                ConceptContentRewriteAgent,
                {
                    **concept.model_dump(mode="json"),
                    "quality_findings": _quality_findings_from_artifact_issues(quality_issues),
                    "language": cfg.language,
                    "book_notes": cfg.book_notes,
                    "allowed_source_refs": sorted(allowed_refs),
                },
                model=cfg.model_for("quality_rewrite"),
                cache_dir=_cache_dir(cfg),
                force=True,
                runtime=cfg.llm_runtime,
            )
            cache_results.append(rewritten)
            candidate = rewritten.result
            if _body_too_short(candidate.body_md, concept.body_md):
                _LOG.warning(
                    "discarding concept quality rewrite for %s: body shrank from %d to %d chars",
                    concept.name,
                    len(concept.body_md),
                    len(candidate.body_md),
                )
                continue
            concept = candidate
            continue
        break

    final_concept = best_concept if best_issues is not None else concept
    final_issues = best_issues if best_issues is not None else []
    return (
        final_concept,
        cache_results,
        Issue(
            severity="warning",
            code="CONCEPT_VALIDATION_UNRESOLVED",
            message=(
                f"{final_concept.name} concept kept after inline validation rounds: "
                f"{'; '.join(issue.message for issue in final_issues)}"
            ),
            owner_task_id=str(final_concept.owner_task_id or f"concept:{final_concept.name}"),
        ),
    )


def _allowed_refs_from_concept_contexts(chapter_contexts: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for context in chapter_contexts:
        refs.update(SOURCE_REF_RE.findall(str(context.get("source_md") or "")))
    return refs


def _quality_findings_from_artifact_issues(
    issues: list[ArtifactIssue],
) -> list[dict[str, str]]:
    return [
        {"quote": issue.quote, "explanation": issue.explanation or "language_leak"}
        for issue in issues
        if issue.kind == "quality"
    ]


__all__ = [
    "_unique_file_stem",
    "_concept_contexts",
    "reconcile_node",
    "_merge_candidates_with_skeleton",
    "concept_pages_node",
    "prepare_concept_pages_fanout_node",
    "concept_page_fanout_specs",
    "_concept_item_name",
    "concept_page_fanout_node",
    "_run_concept_page_unit",
    "collect_concept_pages_fanout_node",
    "_issue_concept_name",
    "_validate_concept_artifact_inline",
    "_allowed_refs_from_concept_contexts",
    "_quality_findings_from_artifact_issues",
]
