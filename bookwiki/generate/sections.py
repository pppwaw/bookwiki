"""Chapter generation as a section pipeline (Phase 3, agentic generate).

This module replaces the legacy single-call lesson agent per chapter with a
plan / generate / validate / repair / assemble loop:

    plan_sections          -> SectionPlan (teaching units)
    for each section (parallel, bounded by maxSectionConcurrency):
        generate_one_section    -> SectionResult (prose + flat metadata, with
                                   knowledge quizzes authored inline as <QuizBlock>s)
        sanitize_inline_quizzes -> validate/canonicalize the authored <QuizItem>s and
                                   collect the application <QuizItemSlot/> specs
        validate_section     -> deterministic checks vs the book skeleton
        repair_section        -> up to ``maxSectionRepairRounds`` retries
        (fallback)            -> record a warning Issue, keep the imperfect body
    assemble_chapter_result -> ChapterResult (full body_md)
    ApplicationQuizAgent    -> fill each collected <QuizItemSlot/> per-slot
    CardAgent               -> recall cards from the assembled body
    SummaryAgent            -> chapter summary

Sections fan out with ``asyncio.gather`` (order preserved) bounded by a
``cfg.section_concurrency`` semaphore: a section's input depends only on the static
plan (outline + position), never on a sibling section's body, so there is no data
dependency. Shared LLM rate limiting is owned by the single injected runtime's Router.

It is intentionally plain ``async`` Python (no compiled LangGraph subgraph): caching
lives at the ``run_with_cache`` level and the parent graph checkpointer only sees the
``generate`` node, so a subgraph buys no extra recovery here. Phase 5 can wrap
``generate_chapter_sections`` as a ``Send`` fan-out unit without changing its I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.agents.application_quiz_agent import ApplicationQuizAgent, WorkedApplicationQuizAgent
from bookwiki.agents.card_agent import CardAgent
from bookwiki.agents.chapter_content_rewrite_agent import ChapterContentRewriteAgent
from bookwiki.agents.mdx_edit_repair import ChapterMdxEditRepairAgent
from bookwiki.agents.prompting import prompt_cache_key
from bookwiki.agents.repair_section_agent import RepairSectionAgent
from bookwiki.agents.section_agent import SectionAgent
from bookwiki.agents.section_planner_agent import SectionPlannerAgent
from bookwiki.agents.summary_agent import SummaryAgent
from bookwiki.agents.supplement_image_agent import SupplementImageAgent
from bookwiki.checkers.mdx_validator import validate_mdx
from bookwiki.generate.figures import (
    build_book_figure_tag,
    generated_asset_relpath,
    public_asset_url,
    reuse_existing_figure,
    run_plot,
    verify_figure,
)
from bookwiki.generate.inline_quiz import (
    SlotSpec,
    sanitize_inline_quizzes,
    strip_inline_quizzes_and_control_slots,
)
from bookwiki.generate.validate_artifact import ArtifactIssue, validate_artifact
from bookwiki.integrator.markdown_renderers import normalize_mdx_for_validation
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import build_runtime
from bookwiki.schemas.card import CardResult
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import Citation
from bookwiki.schemas.figure import ImageSupplementResult
from bookwiki.schemas.quiz import QuizItem, QuizResult, WorkedItem
from bookwiki.schemas.report import Issue
from bookwiki.schemas.section import SectionPlan, SectionResult, SectionSpec
from bookwiki.schemas.summary import SummaryResult
from bookwiki.utils.files import read_json, write_json
from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_MAX_SECTION_REPAIR_ROUNDS = 2
DEFAULT_PLOT_TIMEOUT_SECONDS = 30
# A repair/rewrite that shrinks a body below this fraction of the previous body is
# treated as a CATASTROPHIC truncation (LLM dropped most of the content / returned a
# stub) and discarded; the previous artifact is kept and the round is still consumed.
# Deliberately lenient (1/3): legitimate quality rewrites that strip leaked English and
# replace it with concise target-language prose can shrink a body by ~half, so this is a
# truncation tripwire only, NOT a fine-grained content-preservation check (that is what
# best-result tracking + re-validation handle).
MIN_REPAIR_BODY_RATIO = 0.34


def _body_too_short(new_body: str, prev_body: str) -> bool:
    prev_len = len(prev_body)
    if prev_len == 0:
        return False
    return len(new_body) < MIN_REPAIR_BODY_RATIO * prev_len


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
    ordered_specs = sorted(section_plan.sections, key=lambda item: item.index)
    # Inject the chapter's own outline into every section call so a section knows
    # what the rest of THIS chapter covers (and its own position). Without it a
    # section mistakes a later same-chapter topic for "the next chapter".
    chapter_outline = [
        {"index": spec.index, "title": spec.title, "learning_goal": spec.learning_goal}
        for spec in ordered_specs
    ]

    # Sections within a chapter generate in parallel (bounded by
    # ``cfg.section_concurrency``); each section's input depends only on the static
    # plan (outline + position), not on sibling section bodies, so there is no data
    # dependency. ``asyncio.gather`` preserves order, keeping cache_results / issues /
    # assembly deterministic. Shared LLM rate limiting is handled by the single
    # injected runtime's Router (see lg_runner), so this does not multiply API pressure
    # beyond the configured tpm/rpm.
    section_semaphore = asyncio.Semaphore(cfg.section_concurrency)

    async def run_section(spec: SectionSpec):
        async with section_semaphore:
            return await _generate_validated_section(
                cfg=cfg,
                base_payload=base_payload,
                spec=spec,
                figures=figures,
                skeleton_payload=skeleton_payload,
                allowed_refs=allowed_refs,
                chapter_outline=chapter_outline,
            )

    section_outcomes = await asyncio.gather(*(run_section(spec) for spec in ordered_specs))

    sections: list[SectionResult] = []
    section_slot_specs: list[SlotSpec] = []
    for section, slot_specs, section_cache, section_issue in section_outcomes:
        cache_results.extend(section_cache)
        if section_issue is not None:
            issues.append(section_issue)
        sections.append(section)
        section_slot_specs.extend(slot_specs)

    # Figure supplementation also fans out per section (each request is independent).
    figure_semaphore = asyncio.Semaphore(cfg.section_concurrency)

    async def run_figures(section: SectionResult):
        async with figure_semaphore:
            return await supplement_section_figures(
                cfg=cfg,
                chapter_id=chapter_id,
                section=section,
                source_figures=figures,
            )

    figure_outcomes = await asyncio.gather(*(run_figures(section) for section in sections))

    generated_figures: dict[str, str] = {}
    for section_registry, figure_issues in figure_outcomes:
        generated_figures.update(section_registry)
        issues.extend(figure_issues)

    chapter = assemble_chapter_result(chapter_id=chapter_id, title=title, sections=sections)
    chapter, chapter_cache, chapter_issue = await _validate_chapter_artifact_inline(
        cfg=cfg,
        base_payload=base_payload,
        chapter=chapter,
        allowed_refs=allowed_refs,
    )
    cache_results.extend(chapter_cache)
    if chapter_issue is not None:
        issues.append(chapter_issue)

    # Knowledge quizzes are authored inline and already sanitized per-section; only the
    # application <QuizItemSlot/>s need filling. Feed the collected slot specs to the quiz
    # agent, which binds each generated item back to its canonical slot id by order. When no
    # section declared an application slot, skip the (pro-model) agent entirely.
    application_requests = [
        {
            "slot_id": spec.slot_id,
            "topic": spec.topic,
            "concept": spec.concept,
            "source_refs": spec.source_refs,
            "kind": spec.kind,
        }
        for spec in section_slot_specs
    ]
    if application_requests:
        application_quiz, application_cache, application_issue = await _generate_application_quiz(
            cfg=cfg,
            base_payload=base_payload,
            chapter=chapter,
            requests=application_requests,
            allowed_refs=allowed_refs,
        )
        cache_results.extend(application_cache)
        if application_issue is not None:
            issues.append(application_issue)
    else:
        application_quiz = QuizResult(
            chapter_id=chapter_id, items=[], owner_task_id=f"{chapter_id}:quiz"
        )
    quiz = application_quiz

    # Card/Summary must not see authored quiz blocks or slot placeholders as prose to echo.
    downstream_body = strip_inline_quizzes_and_control_slots(chapter.body_md)

    card_result = await run_with_cache(
        CardAgent,
        {
            **base_payload,
            "cards_per_chapter": cfg.cards_per_chapter,
            "chapter_body_md": downstream_body,
        },
        model=cfg.model_for("card"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )
    cache_results.append(card_result)
    card: CardResult = card_result.result

    summary = await run_with_cache(
        SummaryAgent,
        {
            **base_payload,
            "chapter_result": chapter.model_dump(mode="json"),
            "chapter_body_md": downstream_body,
            "chapter_outline": chapter_outline,
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


async def _validate_chapter_artifact_inline(
    *,
    cfg: BookConfig,
    base_payload: dict[str, Any],
    chapter: ChapterResult,
    allowed_refs: set[str],
) -> tuple[ChapterResult, list[CacheResult], Issue | None]:
    del base_payload
    cache_results: list[CacheResult] = []
    max_mdx_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    max_quality_rounds = int(cfg.generation.get("maxQualityRounds", 1) or 1)
    mdx_rounds = 0
    quality_rounds = 0
    best_chapter = chapter
    best_issues: list[ArtifactIssue] | None = None

    while True:
        issues = await validate_artifact(
            body_md=chapter.body_md,
            kind="chapter",
            allowed_refs=allowed_refs,
            cfg=cfg,
        )
        if not issues:
            return chapter, cache_results, None
        # Track the fewest-issue version seen so a later, worse repair round does not
        # clobber an earlier, better one when the loop bottoms out.
        if best_issues is None or len(issues) < len(best_issues):
            best_chapter = chapter
            best_issues = issues

        mdx_issues = [issue for issue in issues if issue.kind == "mdx"]
        quality_issues = [issue for issue in issues if issue.kind == "quality"]
        if mdx_issues:
            if mdx_rounds >= max_mdx_rounds:
                break
            mdx_rounds += 1
            repaired = await run_with_cache(
                ChapterMdxEditRepairAgent,
                {
                    **chapter.model_dump(mode="json"),
                    "mdx_errors": [issue.message for issue in mdx_issues],
                    "language": cfg.language,
                    "book_notes": cfg.book_notes,
                    "allowed_source_refs": sorted(allowed_refs),
                },
                model=cfg.model_for("mdx_repair"),
                cache_dir=cfg.cache_dir / "tasks",
                force=True,
                runtime=cfg.llm_runtime,
            )
            cache_results.append(repaired)
            candidate = repaired.result
            if _body_too_short(candidate.body_md, chapter.body_md):
                LOGGER.warning(
                    "discarding chapter MDX repair for %s: body shrank from %d to %d chars",
                    chapter.chapter_id,
                    len(chapter.body_md),
                    len(candidate.body_md),
                )
                continue
            chapter = candidate
            continue
        if quality_issues:
            if quality_rounds >= max_quality_rounds:
                break
            quality_rounds += 1
            rewritten = await run_with_cache(
                ChapterContentRewriteAgent,
                {
                    **chapter.model_dump(mode="json"),
                    "quality_findings": _quality_findings_from_artifact_issues(quality_issues),
                    "language": cfg.language,
                    "book_notes": cfg.book_notes,
                    "allowed_source_refs": sorted(allowed_refs),
                },
                model=cfg.model_for("quality_rewrite"),
                cache_dir=cfg.cache_dir / "tasks",
                force=True,
                runtime=cfg.llm_runtime,
            )
            cache_results.append(rewritten)
            candidate = rewritten.result
            if _body_too_short(candidate.body_md, chapter.body_md):
                LOGGER.warning(
                    "discarding chapter quality rewrite for %s: body shrank from %d to %d chars",
                    chapter.chapter_id,
                    len(chapter.body_md),
                    len(candidate.body_md),
                )
                continue
            chapter = candidate
            continue
        break

    # Exhausted: keep the fewest-issue version seen, not necessarily the last round.
    final_chapter = best_chapter if best_issues is not None else chapter
    final_issues = best_issues if best_issues is not None else []
    return (
        final_chapter,
        cache_results,
        Issue(
            severity="warning",
            code="CHAPTER_VALIDATION_UNRESOLVED",
            message=(
                f"{final_chapter.chapter_id} chapter kept after inline validation rounds: "
                f"{'; '.join(issue.message for issue in final_issues)}"
            ),
            owner_task_id=f"{final_chapter.chapter_id}:chapter",
        ),
    )


def _quality_findings_from_artifact_issues(
    issues: list[ArtifactIssue],
) -> list[dict[str, str]]:
    return [
        {"quote": issue.quote, "explanation": issue.explanation or "language_leak"}
        for issue in issues
        if issue.kind == "quality"
    ]


async def _generate_application_quiz(
    *,
    cfg: BookConfig,
    base_payload: dict[str, Any],
    chapter: ChapterResult,
    requests: list[dict[str, Any]],
    allowed_refs: set[str],
) -> tuple[QuizResult, list[CacheResult], Issue | None]:
    # Fill each application <QuizItemSlot/> with its OWN agent call. Per-slot beats one
    # batched call: the slot id is known (no fragile by-order binding), the task cache keys on
    # the single request (granular invalidation), MDX repair only re-runs the failing slot, and
    # the repeated chapter body hits provider context caching. Bounded by the same concurrency
    # as sections; the shared runtime Router still enforces tpm/rpm.
    semaphore = asyncio.Semaphore(cfg.section_concurrency)
    # Strip the chapter body once: it is identical for every slot (and every repair round),
    # and stripping runs the Node MDX extractor — doing it per-slot would re-spawn that
    # subprocess N×(1+repairRounds) times for no benefit.
    chapter_body_stripped = strip_inline_quizzes_and_control_slots(chapter.body_md)

    async def fill(request: dict[str, Any]):
        async with semaphore:
            return await _fill_application_slot(
                cfg=cfg,
                base_payload=base_payload,
                chapter_body_stripped=chapter_body_stripped,
                request=request,
                allowed_refs=allowed_refs,
            )

    outcomes = await asyncio.gather(*(fill(request) for request in requests))
    items: list[QuizItem] = []
    worked_items: list[WorkedItem] = []
    cache_results: list[CacheResult] = []
    unresolved: list[str] = []
    for slot_items, slot_worked_items, slot_cache, slot_errors in outcomes:
        items.extend(slot_items)
        worked_items.extend(slot_worked_items)
        cache_results.extend(slot_cache)
        unresolved.extend(slot_errors)

    quiz = QuizResult(
        chapter_id=chapter.chapter_id,
        items=items,
        worked_items=worked_items,
        owner_task_id=f"{chapter.chapter_id}:quiz",
    )
    issue: Issue | None = None
    if unresolved:
        issue = Issue(
            severity="warning",
            code="QUIZ_VALIDATION_UNRESOLVED",
            message=(
                f"{chapter.chapter_id} application quiz kept after inline validation rounds: "
                f"{'; '.join(unresolved)}"
            ),
            owner_task_id=f"{chapter.chapter_id}:quiz",
        )
    return quiz, cache_results, issue


async def _fill_application_slot(
    *,
    cfg: BookConfig,
    base_payload: dict[str, Any],
    chapter_body_stripped: str,
    request: dict[str, Any],
    allowed_refs: set[str],
) -> tuple[list[QuizItem], list[WorkedItem], list[CacheResult], list[str]]:
    """Generate one application quiz item for a single slot, with a bounded MDX repair loop.

    The agent returns one slot-agnostic ``QuizItem``; this slot's canonical ``slot_id`` is
    stamped on here, so there is no ordering or by-position binding to get wrong.
    """
    cache_results: list[CacheResult] = []
    run = await _run_application_quiz(
        cfg=cfg,
        base_payload=base_payload,
        chapter_body_stripped=chapter_body_stripped,
        request=request,
        allowed_refs=allowed_refs,
        mdx_errors=[],
        force=False,
    )
    cache_results.append(run)
    item: QuizItem | WorkedItem = run.result
    errors = _application_quiz_mdx_errors([item])
    max_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    rounds = 0
    while errors and rounds < max_rounds:
        rounds += 1
        repaired = await _run_application_quiz(
            cfg=cfg,
            base_payload=base_payload,
            chapter_body_stripped=chapter_body_stripped,
            request=request,
            allowed_refs=allowed_refs,
            mdx_errors=errors,
            force=True,
        )
        cache_results.append(repaired)
        item = repaired.result
        errors = _application_quiz_mdx_errors([item])
    filled = item.model_copy(update={"slot_id": str(request.get("slot_id") or "")})
    if isinstance(filled, WorkedItem):
        return [], [filled], cache_results, errors
    return [filled], [], cache_results, errors


async def _run_application_quiz(
    *,
    cfg: BookConfig,
    base_payload: dict[str, Any],
    chapter_body_stripped: str,
    request: dict[str, Any],
    allowed_refs: set[str],
    mdx_errors: list[str],
    force: bool,
) -> CacheResult:
    slot_kind = str(request.get("kind") or "mcq")
    agent_cls = WorkedApplicationQuizAgent if slot_kind == "worked" else ApplicationQuizAgent
    return await run_with_cache(
        agent_cls,
        {
            **base_payload,
            "chapter_body_md": chapter_body_stripped,
            "request": request,
            "allowed_source_refs": sorted(allowed_refs),
            "mdx_errors": mdx_errors,
        },
        model=cfg.model_for(agent_cls.model_key),
        cache_dir=cfg.cache_dir / "tasks",
        force=force,
        runtime=cfg.llm_runtime,
    )


def _application_quiz_mdx_errors(items: list[QuizItem | WorkedItem]) -> list[str]:
    errors: list[str] = []
    for item_index, item in enumerate(items, start=1):
        fields = [("question", item.question), ("explanation", item.explanation)]
        if isinstance(item, WorkedItem):
            fields.append(("reference_answer", item.reference_answer))
            fields.extend(
                (f"rubric {rubric_index}", point.point)
                for rubric_index, point in enumerate(item.rubric, 1)
            )
        else:
            fields.extend(
                (f"choice {choice_index}", choice)
                for choice_index, choice in enumerate(item.choices, 1)
            )
        for field_name, text in fields:
            for error in validate_mdx(normalize_mdx_for_validation(str(text))):
                errors.append(f"item {item_index} {field_name}: {error}")
    return errors


async def _plan_sections(cfg: BookConfig, payload: dict[str, Any]) -> CacheResult:
    return await run_with_cache(
        SectionPlannerAgent,
        payload,
        model=cfg.model_for("section_planner"),
        cache_dir=cfg.cache_dir / "tasks",
        runtime=cfg.llm_runtime,
    )


def _section_position(spec: SectionSpec, chapter_outline: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a section's position flags within the chapter outline.

    ``is_first``/``is_last`` compare against the actual first/last outline index, not a
    hardcoded 0, so a plan numbered from a non-zero base still flags the boundaries.
    """
    first_index = chapter_outline[0]["index"] if chapter_outline else spec.index
    last_index = chapter_outline[-1]["index"] if chapter_outline else spec.index
    return {
        "index": spec.index,
        "total": len(chapter_outline),
        "is_first": spec.index == first_index,
        "is_last": spec.index == last_index,
    }


async def _generate_validated_section(
    *,
    cfg: BookConfig,
    base_payload: dict[str, Any],
    spec: SectionSpec,
    figures: list[dict[str, str]],
    skeleton_payload: dict[str, Any],
    allowed_refs: set[str],
    chapter_outline: list[dict[str, Any]],
) -> tuple[SectionResult, list[SlotSpec], list[CacheResult], Issue | None]:
    cache_results: list[CacheResult] = []
    section_position = _section_position(spec, chapter_outline)
    section_context = {
        "chapter_outline": chapter_outline,
        "section_position": section_position,
    }
    section_input = {
        **base_payload,
        "section": spec.model_dump(mode="json"),
        "figures": figures,
        **skeleton_payload,
        **section_context,
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
    section = _normalize_section_mdx(section)
    validation = validate_section(
        section=section,
        section_spec=spec,
        allowed_refs=allowed_refs,
        skeleton_payload=skeleton_payload,
    )

    max_rounds = _max_section_repair_rounds(cfg)
    rounds = 0
    best_section = section
    best_validation = validation
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
                **section_context,
            },
            model=cfg.model_for("section_repair"),
            cache_dir=cfg.cache_dir / "tasks",
            runtime=cfg.llm_runtime,
        )
        cache_results.append(repaired)
        candidate = repaired.result
        candidate = _normalize_section_mdx(candidate)
        if _body_too_short(candidate.body_md, section.body_md):
            LOGGER.warning(
                "discarding section repair for %s section %d: body shrank from %d to %d chars",
                spec.chapter_id,
                spec.index,
                len(section.body_md),
                len(candidate.body_md),
            )
            continue
        section = candidate
        validation = validate_section(
            section=section,
            section_spec=spec,
            allowed_refs=allowed_refs,
            skeleton_payload=skeleton_payload,
        )
        # Keep the fewest-issue version so a worse later round can't clobber a better one.
        if len(validation.messages) < len(best_validation.messages):
            best_section = section
            best_validation = validation

    if not validation.ok and len(best_validation.messages) < len(validation.messages):
        section = best_section
        validation = best_validation

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
    # Knowledge quizzes are authored inline by SectionAgent; validate/canonicalize them and
    # collect the application <QuizItemSlot/> specs (with canonical slot ids) for later fill.
    sanitized = sanitize_inline_quizzes(
        section.body_md,
        allowed_refs=allowed_refs,
        chapter_id=spec.chapter_id,
        section_index=section.section_index,
    )
    for warning in sanitized.warnings:
        LOGGER.warning("inline quiz sanitize: %s", warning)
    section = section.model_copy(update={"body_md": sanitized.body_md})
    return section, sanitized.slot_specs, cache_results, issue


def _normalize_section_mdx(section: SectionResult) -> SectionResult:
    body_md = normalize_mdx_for_validation(section.body_md)
    if body_md == section.body_md:
        return section
    return section.model_copy(update={"body_md": body_md})


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
    alias_map = skeleton_payload.get("alias_map_slice", {}) or {}
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

    # Result cache: SupplementImageAgent runs an expensive multi-round tool loop and is
    # NOT routed through run_with_cache, so without this a rerun re-burns the whole loop.
    # Keyed by the inputs that determine the figure; only ok=True results are cached, and
    # the cached image must still be present and verifiable to be reused.
    model = cfg.model_for("supplement_image")
    cache_key = _supplement_cache_key(
        chapter_id=chapter_id,
        figure_ref=figure_ref,
        rationale=str(request.rationale),
        model=model,
    )
    sidecar = cfg.cache_dir / "figures" / f"{cache_key}.json"
    cached = _reuse_cached_supplement(sidecar, out_abs, figure_ref)
    if cached is not None:
        return cached, ""

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
            # run_plot returns image_path = str(out_abs) (already book_dir-prefixed,
            # resolved against cwd); use it as-is. Re-joining cfg.book_dir here would
            # double the prefix (books/mini/books/mini/...) → spurious "file does not exist".
            return verify_figure(str(args.get("image_path", "")))
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
        model=model,
        runtime=_runtime_for(cfg),
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
    _write_supplement_sidecar(sidecar, image_relpath=image_relpath, caption=caption)
    return tag, ""


def _supplement_cache_key(*, chapter_id: str, figure_ref: str, rationale: str, model: str) -> str:
    payload = json.dumps(
        {
            "chapter_id": chapter_id,
            "figure_ref": figure_ref,
            "rationale": rationale,
            "model": model,
            "prompt": prompt_cache_key(SupplementImageAgent.prompt_template),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _reuse_cached_supplement(sidecar: Any, out_abs: Any, figure_ref: str) -> str | None:
    """Return a <BookFigure/> tag from a cached supplement, or ``None`` to regenerate.

    Reuse requires both the sidecar metadata AND the generated image still present and
    verifiable - a stale sidecar pointing at a missing/corrupt image regenerates.
    """
    if not sidecar.exists() or not out_abs.exists():
        return None
    if not verify_figure(out_abs)["ok"]:
        return None
    record = read_json(sidecar, default={})
    image_relpath = record.get("image_relpath")
    caption = record.get("caption")
    if not image_relpath or not caption:
        return None
    return build_book_figure_tag(figure_ref, src=public_asset_url(image_relpath), caption=caption)


def _write_supplement_sidecar(sidecar: Any, *, image_relpath: str, caption: str) -> None:
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    write_json(sidecar, {"image_relpath": image_relpath, "caption": caption})


def _figure_issue(chapter_id: str, section_index: int, error: str) -> Issue:
    return Issue(
        severity="warning",
        code="FIGURE_SUPPLEMENT_FAILED",
        message=f"{chapter_id} section {section_index} figure unresolved: {error}",
        owner_task_id=f"{chapter_id}:chapter",
    )


def _runtime_for(cfg: BookConfig):
    """Return the shared pipeline runtime, warning if we must build an ad-hoc one.

    ``SupplementImageAgent`` is invoked directly (not via ``run_with_cache``), so it
    needs the runtime passed explicitly. The shared runtime is injected on ``cfg`` by
    ``lg_runner``; a missing one means an unwired call path and is worth a warning
    because it would build a throwaway Router (no shared tpm/rpm or cost accounting).
    """
    if cfg.llm_runtime is not None:
        return cfg.llm_runtime
    LOGGER.warning("no shared runtime injected for SupplementImageAgent; building ad-hoc runtime")
    return build_runtime()


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
