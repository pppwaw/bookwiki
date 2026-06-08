"""Chapter generation as a serial section pipeline (Phase 3, agentic generate).

This module replaces the legacy single-call lesson agent per chapter with a
plan / generate / validate / repair / assemble loop:

    plan_sections          -> SectionPlan (teaching units)
    for each section:
        generate_one_section -> SectionResult (prose only)
        validate_section     -> deterministic checks vs the book skeleton
        repair_section        -> up to ``maxSectionRepairRounds`` retries
        (fallback)            -> record a warning Issue, keep the imperfect body
    assemble_chapter_result -> ChapterResult (full body_md)
    QuizCardAgent           -> quiz + card from the assembled body
    SummaryAgent            -> chapter summary

It is intentionally plain ``async`` Python (a ``while`` loop, no compiled
LangGraph subgraph): caching lives at the ``run_with_cache`` level and the parent
graph checkpointer only sees the ``generate`` node, so a subgraph buys no extra
recovery here. Phase 5 can wrap ``generate_chapter_sections`` as a ``Send``
fan-out unit without changing its inputs or outputs.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.agents.quiz_card_agent import QuizCardAgent, chapter_body_blocks
from bookwiki.agents.repair_section_agent import RepairSectionAgent
from bookwiki.agents.section_agent import SectionAgent
from bookwiki.agents.section_planner_agent import SectionPlannerAgent
from bookwiki.agents.summary_agent import SummaryAgent
from bookwiki.agents.supplement_image_agent import SupplementImageAgent
from bookwiki.generate.figures import (
    build_book_figure_tag,
    generated_asset_relpath,
    public_asset_url,
    reuse_existing_figure,
    run_plot,
    verify_figure,
)
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import build_runtime
from bookwiki.schemas.card import CardResult
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import Citation
from bookwiki.schemas.figure import ImageSupplementResult
from bookwiki.schemas.quiz import QuizPlacement, QuizResult
from bookwiki.schemas.report import Issue
from bookwiki.schemas.section import SectionPlan, SectionResult, SectionSpec
from bookwiki.schemas.summary import SummaryResult

DEFAULT_MAX_SECTION_REPAIR_ROUNDS = 2
DEFAULT_PLOT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SectionValidation:
    """Outcome of the deterministic section-level checks."""

    ok: bool
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChapterGenerationResult:
    """Everything ``generate_node`` needs to persist one chapter's artifacts."""

    chapter: ChapterResult
    quiz: QuizResult
    card: CardResult
    summary: SummaryResult
    issues: list[Issue]
    generated_figures: dict[str, str]
    cache_hit: bool


async def generate_chapter_sections(
    *,
    cfg: BookConfig,
    chapter_id: str,
    title: str,
    source_md: str,
    source_path: str,
    topics: list[str],
    figures: list[dict[str, str]],
    skeleton_payload: dict[str, Any],
) -> ChapterGenerationResult:
    """Generate one chapter section-by-section and bundle the four artifacts."""
    base_payload: dict[str, Any] = {
        "chapter_id": chapter_id,
        "title": title,
        "source_md": source_md,
        "source_path": source_path,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
    }
    allowed_refs = set(SOURCE_REF_RE.findall(source_md))
    cache_results: list[CacheResult] = []
    issues: list[Issue] = []

    plan = await _plan_sections(cfg, {**base_payload, "topics": topics, **skeleton_payload})
    cache_results.append(plan)
    section_plan: SectionPlan = plan.result

    sections: list[SectionResult] = []
    for spec in sorted(section_plan.sections, key=lambda item: item.index):
        section, section_cache, section_issue = await _generate_validated_section(
            cfg=cfg,
            base_payload=base_payload,
            spec=spec,
            figures=figures,
            skeleton_payload=skeleton_payload,
            allowed_refs=allowed_refs,
        )
        cache_results.extend(section_cache)
        if section_issue is not None:
            issues.append(section_issue)
        sections.append(section)

    generated_figures: dict[str, str] = {}
    for section in sections:
        section_registry, figure_issues = await supplement_section_figures(
            cfg=cfg,
            chapter_id=chapter_id,
            section=section,
            source_figures=figures,
        )
        generated_figures.update(section_registry)
        issues.extend(figure_issues)

    chapter = assemble_chapter_result(chapter_id=chapter_id, title=title, sections=sections)

    quiz_card = await run_with_cache(
        QuizCardAgent,
        {
            **base_payload,
            "quiz_per_chapter": cfg.quiz_per_chapter,
            "cards_per_chapter": cfg.cards_per_chapter,
            "chapter_body_md": chapter.body_md,
        },
        model=cfg.model_for("quiz_card"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )
    cache_results.append(quiz_card)
    quiz = _normalize_quiz_placements(quiz_card.result.quiz, chapter.body_md)
    card: CardResult = quiz_card.result.card

    summary = await run_with_cache(
        SummaryAgent,
        {
            **base_payload,
            "chapter_result": chapter.model_dump(mode="json"),
            "chapter_body_md": chapter.body_md,
        },
        model=cfg.model_for("summary"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )
    cache_results.append(summary)

    return ChapterGenerationResult(
        chapter=chapter,
        quiz=quiz,
        card=card,
        summary=summary.result,
        issues=issues,
        generated_figures=generated_figures,
        cache_hit=bool(cache_results) and all(item.cache_hit for item in cache_results),
    )


async def _plan_sections(cfg: BookConfig, payload: dict[str, Any]) -> CacheResult:
    return await run_with_cache(
        SectionPlannerAgent,
        payload,
        model=cfg.model_for("section_planner"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )


async def _generate_validated_section(
    *,
    cfg: BookConfig,
    base_payload: dict[str, Any],
    spec: SectionSpec,
    figures: list[dict[str, str]],
    skeleton_payload: dict[str, Any],
    allowed_refs: set[str],
) -> tuple[SectionResult, list[CacheResult], Issue | None]:
    cache_results: list[CacheResult] = []
    section_input = {
        **base_payload,
        "section": spec.model_dump(mode="json"),
        "figures": figures,
        **skeleton_payload,
    }
    generated = await run_with_cache(
        SectionAgent,
        section_input,
        model=cfg.model_for("section"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )
    cache_results.append(generated)
    section: SectionResult = generated.result
    validation = validate_section(
        section=section,
        section_spec=spec,
        allowed_refs=allowed_refs,
        skeleton_payload=skeleton_payload,
    )

    max_rounds = _max_section_repair_rounds(cfg)
    rounds = 0
    while not validation.ok and rounds < max_rounds:
        rounds += 1
        repaired = await run_with_cache(
            RepairSectionAgent,
            {
                **base_payload,
                "section": spec.model_dump(mode="json"),
                "previous_section": section.model_dump(mode="json"),
                "issues": list(validation.messages),
                **skeleton_payload,
            },
            model=cfg.model_for("section_repair"),
            cache_dir=cfg.cache_dir / "tasks",
            runtime=cfg.llm_runtime,
        )
        cache_results.append(repaired)
        section = repaired.result
        validation = validate_section(
            section=section,
            section_spec=spec,
            allowed_refs=allowed_refs,
            skeleton_payload=skeleton_payload,
        )

    issue: Issue | None = None
    if not validation.ok:
        issue = Issue(
            severity="warning",
            code="SECTION_VALIDATION_UNRESOLVED",
            message=(
                f"{spec.chapter_id} section {spec.index} kept after "
                f"{max_rounds} repair rounds: {'; '.join(validation.messages)}"
            ),
            owner_task_id=f"{spec.chapter_id}:chapter",
        )
    return section, cache_results, issue


def validate_section(
    *,
    section: SectionResult,
    section_spec: SectionSpec,
    allowed_refs: set[str],
    skeleton_payload: dict[str, Any],
) -> SectionValidation:
    """Deterministic section checks: citations resolvable + term/ownership compliance."""
    messages: list[str] = []

    for cit in section.citations:
        if allowed_refs and cit.ref_id not in allowed_refs:
            messages.append(f"unknown source_ref {cit.ref_id}")

    uses_by_key = {
        _concept_key(str(entry.get("canonical"))): str(entry.get("canonical"))
        for entry in skeleton_payload.get("chapter_uses", [])
        if isinstance(entry, dict) and entry.get("canonical")
    }
    alias_map = skeleton_payload.get("alias_map", {}) or {}
    for concept in section.concepts:
        key = _concept_key(concept)
        if key in uses_by_key:
            messages.append(f"redefines concept owned by another chapter: {uses_by_key[key]}")
        canonical = alias_map.get(concept) or alias_map.get(key)
        if canonical and canonical != concept:
            messages.append(f"non-canonical term '{concept}', use '{canonical}'")

    return SectionValidation(ok=not messages, messages=messages)


async def supplement_section_figures(
    *,
    cfg: BookConfig,
    chapter_id: str,
    section: SectionResult,
    source_figures: list[dict[str, str]],
) -> tuple[dict[str, str], list[Issue]]:
    """Process a section's ``figure_requests``; return ``(registry, issues)``.

    ``registry`` maps ``figure_id -> canonical <BookFigure/> tag`` for figures
    generated by ``run_plot`` (so the integrator can resolve the section's inline
    placeholder). ``reuse_existing`` needs no entry - the referenced id is already
    in the chapter's source figure index. Best-effort throughout: a failed request
    records a warning ``Issue`` and the unresolved placeholder is dropped at render.
    """
    registry: dict[str, str] = {}
    issues: list[Issue] = []
    for request in section.figure_requests:
        kind = (request.kind or "none").strip()
        figure_ref = (request.figure_ref or "").strip()
        if not figure_ref or kind in {"", "none"}:
            continue
        if kind == "reuse_existing":
            outcome = reuse_existing_figure(figure_ref, source_figures)
            if not outcome["ok"]:
                issues.append(_figure_issue(chapter_id, section.section_index, outcome["error"]))
            continue
        if kind == "plot":
            tag, error = await _supplement_plot(
                cfg=cfg, chapter_id=chapter_id, section=section, request=request
            )
            if tag:
                registry[figure_ref] = tag
            else:
                issues.append(_figure_issue(chapter_id, section.section_index, error))
            continue
        issues.append(
            _figure_issue(chapter_id, section.section_index, f"unknown figure kind {kind!r}")
        )
    return registry, issues


async def _supplement_plot(
    *,
    cfg: BookConfig,
    chapter_id: str,
    section: SectionResult,
    request: Any,
) -> tuple[str, str]:
    figure_ref = str(request.figure_ref).strip()
    out_rel = generated_asset_relpath(chapter_id, figure_ref)
    out_abs = cfg.book_dir / out_rel
    plot_cache = cfg.cache_dir / "plots"
    timeout_s = _plot_timeout(cfg)
    plot_state: dict[str, Any] = {}

    async def tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "run_plot":
            # Run the blocking subprocess off the event loop so parallel chapters
            # keep making progress; concurrent plots are bounded by the chapter
            # concurrency cap (plots happen inside chapters, which are capped).
            result = await asyncio.to_thread(
                run_plot,
                str(args.get("code", "")),
                output_path=out_abs,
                cache_dir=plot_cache,
                timeout_s=timeout_s,
            )
            if result.get("ok"):
                plot_state["image_relpath"] = out_rel
            return result
        if name == "verify_figure":
            return verify_figure(cfg.book_dir / str(args.get("image_path", "")))
        return {"ok": False, "error": f"unknown tool {name!r}"}

    supplement_input = {
        "chapter_id": chapter_id,
        "section_index": section.section_index,
        "section_title": section.title,
        "figure_ref": figure_ref,
        "rationale": request.rationale,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
    }
    result: ImageSupplementResult = await SupplementImageAgent().run(
        supplement_input,
        model=cfg.model_for("supplement_image"),
        runtime=cfg.llm_runtime if cfg.llm_runtime is not None else build_runtime(),
        tool_executor=tool_executor,
    )

    image_relpath = plot_state.get("image_relpath")
    if not image_relpath or not result.ok:
        return "", result.error or "plot did not produce a usable figure"
    verification = verify_figure(out_abs)
    if not verification["ok"]:
        return "", verification["error"]
    caption = result.caption or str(request.rationale)
    tag = build_book_figure_tag(figure_ref, src=public_asset_url(image_relpath), caption=caption)
    return tag, ""


def _figure_issue(chapter_id: str, section_index: int, error: str) -> Issue:
    return Issue(
        severity="warning",
        code="FIGURE_SUPPLEMENT_FAILED",
        message=f"{chapter_id} section {section_index} figure unresolved: {error}",
        owner_task_id=f"{chapter_id}:chapter",
    )


def _plot_timeout(cfg: BookConfig) -> int:
    try:
        value = int(cfg.generation.get("plotTimeoutSeconds", DEFAULT_PLOT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_PLOT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_PLOT_TIMEOUT_SECONDS


def assemble_chapter_result(
    *,
    chapter_id: str,
    title: str,
    sections: list[SectionResult],
) -> ChapterResult:
    """Concatenate section fragments into a full chapter body with a single H1."""
    ordered = sorted(sections, key=lambda item: item.section_index)
    body_parts = [f"# {title}"]
    for section in ordered:
        fragment = _strip_leading_heading(section.body_md)
        if not fragment:
            continue
        body_parts.append(f"## {section.title}\n\n{fragment}".strip())
    return ChapterResult(
        chapter_id=chapter_id,
        title=title,
        body_md="\n\n".join(body_parts).strip(),
        concepts=_dedupe_in_order(concept for section in ordered for concept in section.concepts),
        citations=_dedupe_citations(
            citation for section in ordered for citation in section.citations
        ),
        owner_task_id=f"{chapter_id}:chapter",
    )


def _normalize_quiz_placements(quiz: QuizResult, body_md: str) -> QuizResult:
    """Clamp ``after_block`` into range and drop out-of-range item indexes.

    Conservative defense in depth: rendering (``_insert_quiz_blocks``) already
    clamps and re-homes unassigned items, so this only keeps the stored
    ``quiz.json`` sane without reordering or inventing placements.
    """
    max_after = max(len(chapter_body_blocks(body_md)) - 1, 0)
    item_count = len(quiz.items)
    placements = [
        QuizPlacement(
            after_block=min(max(placement.after_block, 0), max_after),
            item_indexes=[index for index in placement.item_indexes if 1 <= index <= item_count],
            title=placement.title,
        )
        for placement in quiz.placements
    ]
    return quiz.model_copy(update={"placements": placements})


def _max_section_repair_rounds(cfg: BookConfig) -> int:
    try:
        value = int(cfg.generation.get("maxSectionRepairRounds", DEFAULT_MAX_SECTION_REPAIR_ROUNDS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SECTION_REPAIR_ROUNDS
    return value if value >= 0 else DEFAULT_MAX_SECTION_REPAIR_ROUNDS


def _strip_leading_heading(body_md: str) -> str:
    """Drop a single accidental leading markdown heading from a section body.

    The section prompt forbids headings, but defensively removing one leading
    ``#..######`` line prevents duplicated headings once the assembler prepends
    the chapter ``# H1`` and the per-section ``## title``.
    """
    text = str(body_md).strip()
    lines = text.splitlines()
    if lines and re.match(r"^#{1,6}\s+\S", lines[0]):
        return "\n".join(lines[1:]).strip()
    return text


def _dedupe_in_order(values: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _dedupe_citations(citations: Any) -> list[Citation]:
    seen: set[tuple[str, str]] = set()
    out: list[Citation] = []
    for citation in citations:
        key = (citation.ref_id, citation.quote)
        if key not in seen:
            seen.add(key)
            out.append(citation)
    return out


def _concept_key(value: str) -> str:
    """Match :func:`bookwiki.agents.concept_reconcile._concept_key` exactly."""
    return re.sub(r"[\W_]+", "", str(value).casefold(), flags=re.UNICODE)
