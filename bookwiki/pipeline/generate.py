from __future__ import annotations

import asyncio
import re
from html import unescape
from pathlib import Path
from typing import Any

from bookwiki.agents import (
    ApplicationQuizAgent,
    CardAgent,
    ExamAgent,
    ExamExplainAgent,
    SectionAgent,
    SummaryAgent,
)
from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.convert.common import (
    BOOK_FIGURE_TAG_RE,
    parse_book_figure_tag,
)
from bookwiki.generate.exam_pool import build_exam_pools
from bookwiki.generate.sections import generate_chapter_sections
from bookwiki.pipeline._shared import (
    _LOG,
    State,
    _agent_result_payload,
    _display_chapter_title,
    _fanout_semaphores,
    _load_skeleton,
    _rel,
    log_progress,
)
from bookwiki.scheduler.cache import run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.source import DetectedExamQuestion, SourceSummaryResult
from bookwiki.utils.files import ensure_dir, read_json, write_json


def _source_figures(source_md: str) -> list[dict[str, str]]:
    """Extract the de-duplicated ``<BookFigure/>`` references found in ``source_md``.

    Figures are returned in first-seen order keyed by ``id``; captions are
    ``html.unescape``-d so the agent prompt receives human-readable text.
    """
    figures: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in BOOK_FIGURE_TAG_RE.findall(source_md):
        attrs = parse_book_figure_tag(tag)
        figure_id = unescape(attrs.get("id", ""))
        if not figure_id or figure_id in seen:
            continue
        seen.add(figure_id)
        figure: dict[str, str] = {"id": figure_id}
        caption = attrs.get("caption")
        if caption:
            figure["caption"] = unescape(caption)
        figures.append(figure)
    return figures


def _skeleton_payload(skeleton: dict[str, Any] | None, ch_id: str) -> dict[str, Any]:
    """Project the skeleton into the per-chapter section-generation payload.

    Each section generator (``SectionPlannerAgent`` / ``SectionAgent``) receives only a
    **per-chapter slice**, not the whole book's terminology:

    - ``chapter_owns``: concepts whose ``first_chapter_id`` equals ``ch_id`` (this chapter
      owns the definition).
    - ``chapter_uses``: concepts owned by other chapters; only reference, never redefine.
    - ``alias_map_slice``: variant → canonical for *only* the concepts this chapter owns or
      uses (so the section payload's size grows with the chapter, not the whole book). The
      full ``alias_map`` is intentionally NOT shipped here — the integrator still rewrites
      every ``[[alias]]`` deterministically at render time; this slice is the writing-time
      terminology anchor that converts term drift inside prose the integrator cannot catch.
    - ``prev_brief`` / ``next_brief``: neighbouring chapter one-liners for transitions.
    """
    if not skeleton:
        return {}
    glossary = skeleton.get("glossary", []) or []
    full_alias_map = skeleton.get("alias_map", {}) or {}
    chapter_briefs = skeleton.get("chapter_briefs", {}) or {}
    chapter_order: list[str] = list(skeleton.get("chapter_order", []) or [])
    recorded_uses = skeleton.get("chapter_uses", {}) or {}

    owns: list[dict[str, Any]] = []
    not_owned: dict[str, dict[str, Any]] = {}
    for entry in glossary:
        if not isinstance(entry, dict):
            continue
        first = str(entry.get("first_chapter_id") or "")
        projected = {
            "canonical": entry.get("canonical"),
            "aliases": entry.get("aliases", []),
            "first_chapter_id": first,
        }
        if first == ch_id:
            owns.append(projected)
        elif entry.get("canonical"):
            not_owned[_concept_key(str(entry.get("canonical")))] = projected

    # ``chapter_uses`` from the fold names only the concepts this chapter actually
    # references, so the slice shrinks to the chapter. Skeletons without it (legacy /
    # hand-built) fall back to "every concept another chapter owns".
    if ch_id in recorded_uses:
        used_keys = {_concept_key(str(name)) for name in recorded_uses[ch_id]}
        uses = [not_owned[key] for key in not_owned if key in used_keys]
    else:
        uses = list(not_owned.values())

    # Slice the alias map to only the concepts this chapter owns or uses.
    slice_canonicals = {
        str(item.get("canonical")) for item in (*owns, *uses) if item.get("canonical")
    }
    alias_map_slice = {
        variant: canonical
        for variant, canonical in full_alias_map.items()
        if canonical in slice_canonicals
    }

    prev_brief = ""
    next_brief = ""
    if ch_id in chapter_order:
        position = chapter_order.index(ch_id)
        if position > 0:
            prev_brief = chapter_briefs.get(chapter_order[position - 1], "")
        if position + 1 < len(chapter_order):
            next_brief = chapter_briefs.get(chapter_order[position + 1], "")

    return {
        "chapter_owns": owns,
        "chapter_uses": uses,
        "alias_map_slice": alias_map_slice,
        "prev_brief": prev_brief,
        "next_brief": next_brief,
    }


async def generate_node(state: State, cfg: BookConfig) -> State:
    if not state.get("chapter_sources"):
        msg = "generate requires chapter_sources; run split before generate"
        raise ValueError(msg)
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})
    skeleton_data = _load_skeleton(state, cfg)

    section_model = cfg.model_for("section")
    application_quiz_model = cfg.model_for("application_quiz")
    card_model = cfg.model_for("card")
    summary_model = cfg.model_for("summary")

    chapter_items = list(state.get("chapter_sources", {}).items())
    chapter_total = len(chapter_items)
    semaphore = asyncio.Semaphore(cfg.chapter_concurrency)
    targets = cfg.target_chapter_ids
    _LOG.info(
        "generate: chapters=%d concurrency=%d targets=%s",
        chapter_total,
        cfg.chapter_concurrency,
        sorted(targets) if targets else "all",
    )

    async def run_chapter(idx: int, ch_id: str, rel_source: str):
        async with semaphore:
            source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
            title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
            log_progress("generate", idx, chapter_total, "chapter start ch_id=%s", ch_id)
            try:
                result = await generate_chapter_sections(
                    cfg=cfg,
                    chapter_id=ch_id,
                    title=title,
                    source_md=source_md,
                    source_path=rel_source,
                    topics=list(topics_by_chapter.get(ch_id, [])),
                    figures=_source_figures(source_md),
                    skeleton_payload=_skeleton_payload(skeleton_data, ch_id),
                )
            except Exception:
                _LOG.error("generate: chapter failed ch_id=%s", ch_id)
                raise
            log_progress(
                "generate",
                idx,
                chapter_total,
                "chapter done ch_id=%s cache_hit=%s issues=%d figures=%d",
                ch_id,
                result.cache_hit,
                len(result.issues),
                len(result.generated_figures),
            )
            return result

    # Chapters generate in parallel (chapter-level fan-out; sections within a chapter
    # also fan out, bounded by cfg.section_concurrency - see generate.sections),
    # bounded by ``cfg.chapter_concurrency``. ``asyncio.gather`` preserves input order.
    # ``return_exceptions=True`` so one chapter's failure does not discard the
    # in-progress work of its siblings: successful chapters are still written (and
    # cached), then we fail loudly listing the broken chapters so a resume reruns
    # only those.
    generated_list = await asyncio.gather(
        *(
            run_chapter(idx, ch_id, rel_source)
            for idx, (ch_id, rel_source) in enumerate(chapter_items, 1)
        ),
        return_exceptions=True,
    )

    chapter_results: dict[str, dict[str, str]] = {}
    chapter_cache_hits: list[bool] = []
    generation_issues: list[dict[str, Any]] = []
    generated_figures: dict[str, dict[str, str]] = {}
    failures: list[tuple[str, BaseException]] = []
    for (ch_id, _rel_source), generated in zip(chapter_items, generated_list, strict=True):
        if isinstance(generated, BaseException):
            failures.append((ch_id, generated))
            continue
        chapter_cache_hits.append(generated.cache_hit)
        generation_issues.extend(issue.model_dump(mode="json") for issue in generated.issues)
        if generated.generated_figures:
            generated_figures[ch_id] = dict(generated.generated_figures)
        paths = {
            "chapter": write_json(
                result_dir / f"{ch_id}.chapter.json",
                _agent_result_payload(SectionAgent, section_model, generated.chapter),
            ),
            "summary": write_json(
                result_dir / f"{ch_id}.summary.json",
                _agent_result_payload(SummaryAgent, summary_model, generated.summary),
            ),
            "quiz": write_json(
                result_dir / f"{ch_id}.quiz.json",
                _agent_result_payload(ApplicationQuizAgent, application_quiz_model, generated.quiz),
            ),
            "card": write_json(
                result_dir / f"{ch_id}.card.json",
                _agent_result_payload(CardAgent, card_model, generated.card),
            ),
        }
        chapter_results[ch_id] = {name: _rel(path, cfg.book_dir) for name, path in paths.items()}

    if failures:
        failed_ids = [ch_id for ch_id, _exc in failures]
        for ch_id, exc in failures:
            _LOG.error("generate failed for chapter %s: %s", ch_id, exc)
        msg = (
            f"generate failed for chapters: {failed_ids}; "
            f"{len(chapter_results)} chapter(s) completed and were written"
        )
        raise RuntimeError(msg) from failures[0][1]

    _LOG.info(
        "generate: done ok=%d failed=%d issues=%d figures=%d cache_hit=%s",
        len(chapter_results),
        len(failures),
        len(generation_issues),
        len(generated_figures),
        bool(chapter_cache_hits) and all(chapter_cache_hits),
    )
    return {
        "agent_results": chapter_results,
        "generation_issues": generation_issues,
        "generated_figures": generated_figures,
        "generated_figures_index": _persist_generated_figures(cfg, generated_figures),
        "cache_hit": bool(chapter_cache_hits) and all(chapter_cache_hits),
    }


def prepare_generate_fanout_node(state: State, cfg: BookConfig) -> State:
    if not state.get("chapter_sources"):
        msg = "generate requires chapter_sources; run split before generate"
        raise ValueError(msg)
    missing = sorted(cfg.target_chapter_ids - set(state.get("chapter_sources", {})))
    if missing:
        msg = f"generate target chapter(s) not found in split output: {missing}"
        raise ValueError(msg)
    available = list(state.get("chapter_sources", {}))
    targets = cfg.target_chapter_ids
    ensure_dir(cfg.work_dir / "agent_results")
    _LOG.info(
        "prepare_generate: available=%d targets=%s",
        len(available),
        sorted(targets) if targets else "all",
    )
    return {"_generate_parts": None, "cache_hit": False}


def generate_fanout_specs(state: State, cfg: BookConfig) -> list[State]:
    targets = cfg.target_chapter_ids
    selected = [
        (ch_id, rel_source)
        for ch_id, rel_source in state.get("chapter_sources", {}).items()
        if not targets or ch_id in targets
    ]
    total = len(selected)
    return [
        {
            "chapter_sources": state.get("chapter_sources", {}),
            "chapter_titles": state.get("chapter_titles", {}),
            "chapter_topics": state.get("chapter_topics", {}),
            "skeleton": state.get("skeleton"),
            "_fanout_chapter_id": ch_id,
            "_fanout_chapter_source": rel_source,
            "_fanout_index": idx,
            "_fanout_total": total,
        }
        for idx, (ch_id, rel_source) in enumerate(selected, 1)
    ]


def _persist_generated_figures(
    cfg: BookConfig, generated_figures: dict[str, dict[str, str]]
) -> str:
    path = write_json(cfg.work_dir / "generated_figures.json", generated_figures)
    return _rel(path, cfg.book_dir)


async def generate_chapter_fanout_node(state: State, cfg: BookConfig) -> State:
    ch_id = str(state["_fanout_chapter_id"])
    rel_source = str(state["_fanout_chapter_source"])
    idx = int(state.get("_fanout_index", 0))
    total = int(state.get("_fanout_total", 0))
    semaphore = _fanout_semaphores.setdefault(
        (id(cfg), "chapter"), asyncio.Semaphore(cfg.chapter_concurrency)
    )
    async with semaphore:
        log_progress("generate", idx, total, "chapter start ch_id=%s", ch_id)
        try:
            generated = await _run_generate_chapter_unit(state, cfg, ch_id, rel_source)
        except Exception:
            # Let the failure propagate. LangGraph records the *successful* siblings'
            # writes as pending writes against the pre-fanout checkpoint and leaves the
            # super-step uncommitted, so a later ``--resume`` re-runs only this chapter —
            # swallowing the error into a part would advance the checkpoint past the
            # fanout and pin ``--resume`` on the stale error forever.
            _LOG.exception("generate failed for chapter %s", ch_id)
            raise
    log_progress(
        "generate",
        idx,
        total,
        "chapter done ch_id=%s cache_hit=%s issues=%d figures=%d",
        ch_id,
        generated.get("cache_hit"),
        len(generated.get("generation_issues", [])),
        len(generated.get("generated_figures", {})),
    )
    return {"_generate_parts": {ch_id: generated}}


async def _run_generate_chapter_unit(
    state: State, cfg: BookConfig, ch_id: str, rel_source: str
) -> dict[str, Any]:
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})
    skeleton_data = _load_skeleton(state, cfg)
    source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
    title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
    generated = await generate_chapter_sections(
        cfg=cfg,
        chapter_id=ch_id,
        title=title,
        source_md=source_md,
        source_path=rel_source,
        topics=list(topics_by_chapter.get(ch_id, [])),
        figures=_source_figures(source_md),
        skeleton_payload=_skeleton_payload(skeleton_data, ch_id),
    )
    section_model = cfg.model_for("section")
    application_quiz_model = cfg.model_for("application_quiz")
    card_model = cfg.model_for("card")
    summary_model = cfg.model_for("summary")
    paths = {
        "chapter": write_json(
            result_dir / f"{ch_id}.chapter.json",
            _agent_result_payload(SectionAgent, section_model, generated.chapter),
        ),
        "summary": write_json(
            result_dir / f"{ch_id}.summary.json",
            _agent_result_payload(SummaryAgent, summary_model, generated.summary),
        ),
        "quiz": write_json(
            result_dir / f"{ch_id}.quiz.json",
            _agent_result_payload(ApplicationQuizAgent, application_quiz_model, generated.quiz),
        ),
        "card": write_json(
            result_dir / f"{ch_id}.card.json",
            _agent_result_payload(CardAgent, card_model, generated.card),
        ),
    }
    exam_path = await _generate_chapter_exam(
        state, cfg, ch_id, title, source_md, generated.chapter.body_md, result_dir
    )
    if exam_path is not None:
        paths["exam"] = exam_path
    return {
        "chapter_id": ch_id,
        "agent_results": {name: _rel(path, cfg.book_dir) for name, path in paths.items()},
        "generation_issues": [issue.model_dump(mode="json") for issue in generated.issues],
        "generated_figures": dict(generated.generated_figures),
        "cache_hit": generated.cache_hit,
    }


_EXAM_CHAPTER_RE = re.compile(
    r"试卷|期中|期末|考试|真题|测验|exam|midterm|mid-term|final", re.IGNORECASE
)


def _exam_chapter_ids(state: State) -> set[str]:
    """Heuristic, soft detection of past-exam chapters by title / source name.

    Detection only decides which agent runs (walkthrough vs generated exam) and what feeds the
    exam pool; a miss simply means a chapter gets a normal generated exam, so a wrong guess is
    never fatal (see design §3.1).
    """

    titles = state.get("chapter_titles", {})
    sources = state.get("chapter_sources", {})
    ids: set[str] = set()
    for ch_id in sources:
        haystack = f"{ch_id} {titles.get(ch_id, '')} {sources.get(ch_id, '')}"
        if _EXAM_CHAPTER_RE.search(haystack):
            ids.add(str(ch_id))
    return ids


def _chapter_exam_pool(state: State, cfg: BookConfig, ch_id: str) -> list[dict[str, Any]]:
    """This chapter's slice of the past-exam pool, mapped by concept overlap.

    Structure persisted every detected past-exam question to ``work/structure/exam-pool.json``;
    here ``build_exam_pools`` assigns each question to the single best-matching chapter (by the
    chapter's approved topics), and we hand this chapter its slice as ExamAgent 套路 reference.
    """

    path = cfg.work_dir / "structure" / "exam-pool.json"
    if not path.exists():
        return []
    raw = read_json(path).get("questions") or []
    if not raw:
        return []
    summary = SourceSummaryResult(
        source_id="_exam_pool",
        summary_md="",
        is_exam=True,
        exam_questions=[DetectedExamQuestion.model_validate(item) for item in raw],
    )
    chapter_topics = {cid: list(topics) for cid, topics in state.get("chapter_topics", {}).items()}
    pools = build_exam_pools([summary], chapter_topics)
    return [question.model_dump(mode="json") for question in pools.get(ch_id, [])]


async def _generate_chapter_exam(
    state: State,
    cfg: BookConfig,
    ch_id: str,
    title: str,
    source_md: str,
    chapter_body_md: str,
    result_dir: Path,
) -> Path | None:
    """Generate the chapter's exam artifact: a walkthrough for past-paper chapters, otherwise a
    fresh chapter-end exam that borrows from any detected past papers. Returns the written path
    (added to the chapter's ``agent_results`` as ``exam``) or ``None`` on failure."""

    exam_ids = _exam_chapter_ids(state)
    allowed_refs = sorted(set(SOURCE_REF_RE.findall(source_md)))
    payload: dict[str, Any] = {
        "chapter_id": ch_id,
        "title": title,
        "source_md": source_md,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
        "chapter_body_md": chapter_body_md,
        "allowed_source_refs": allowed_refs,
    }
    if ch_id in exam_ids:
        agent_cls: type[Any] = ExamExplainAgent
        model = cfg.models.get("exam_explain") or cfg.model_for("application_quiz")
    else:
        agent_cls = ExamAgent
        model = cfg.models.get("exam") or cfg.model_for("application_quiz")
        payload["exam_pool"] = _chapter_exam_pool(state, cfg, ch_id)
    try:
        result = await run_with_cache(
            agent_cls,
            payload,
            model=model,
            cache_dir=cfg.cache_dir / "tasks",
            runtime=cfg.llm_runtime,
        )
    except Exception as exc:  # noqa: BLE001 - exam is additive; never wedge chapter generation.
        _LOG.warning("exam generation failed ch_id=%s: %s", ch_id, exc)
        return None
    return write_json(
        result_dir / f"{ch_id}.exam.json",
        _agent_result_payload(agent_cls, model, result.result),
    )


def collect_generate_fanout_node(state: State, cfg: BookConfig) -> State:
    parts = state.get("_generate_parts") or {}
    targets = cfg.target_chapter_ids
    # Carry forward prior results ONLY for chapters this run isn't regenerating — i.e.
    # only on a *targeted* run. A full run (no targets) fans out every chapter, so the
    # fresh parts below are authoritative; seeding from ``state`` here would resurrect
    # stale entries left in the channel by a structure change or an earlier run, whose
    # files ``prepare_generate`` may have already cleared.
    chapter_results: dict[str, dict[str, str]] = {
        str(ch_id): dict(paths)
        for ch_id, paths in state.get("agent_results", {}).items()
        if targets and str(ch_id) not in targets
    }
    chapter_cache_hits: list[bool] = []
    generation_issues: list[dict[str, Any]] = [
        dict(issue)
        for issue in state.get("generation_issues", [])
        if targets and _issue_chapter_id(issue) not in targets
    ]
    generated_figures: dict[str, dict[str, str]] = {
        str(ch_id): dict(figures)
        for ch_id, figures in state.get("generated_figures", {}).items()
        if targets and str(ch_id) not in targets
    }
    # A failed chapter worker raises rather than returning a part, so the collect node
    # only ever runs once *every* fanned-out chapter produced a result (LangGraph aborts
    # the super-step otherwise). A missing part therefore signals a real invariant break,
    # not a generation failure — surface it loudly.
    missing: list[str] = []
    chapter_ids = [
        ch_id for ch_id in state.get("chapter_sources", {}) if not targets or ch_id in targets
    ]
    for ch_id in chapter_ids:
        part = parts.get(ch_id)
        if not part or "agent_results" not in part:
            missing.append(ch_id)
            continue
        chapter_results[ch_id] = dict(part["agent_results"])
        chapter_cache_hits.append(bool(part.get("cache_hit", False)))
        generation_issues.extend(part.get("generation_issues", []))
        figures = part.get("generated_figures") or {}
        if figures:
            generated_figures[ch_id] = dict(figures)

    if missing:
        raise RuntimeError(f"generate produced no fanout result for chapters: {missing}")

    _LOG.info(
        "collect_generate: ok=%d issues=%d figures=%d cache_hit=%s",
        len(chapter_results),
        len(generation_issues),
        len(generated_figures),
        bool(chapter_cache_hits) and all(chapter_cache_hits),
    )
    return {
        "agent_results": chapter_results,
        "generation_issues": generation_issues,
        "generated_figures": generated_figures,
        "generated_figures_index": _persist_generated_figures(cfg, generated_figures),
        "cache_hit": bool(chapter_cache_hits) and all(chapter_cache_hits),
    }


def _issue_chapter_id(issue: dict[str, Any]) -> str | None:
    owner = str(issue.get("owner_task_id") or "")
    if ":" not in owner:
        return None
    return owner.split(":", 1)[0]


__all__ = [
    "_source_figures",
    "_skeleton_payload",
    "generate_node",
    "prepare_generate_fanout_node",
    "generate_fanout_specs",
    "_persist_generated_figures",
    "generate_chapter_fanout_node",
    "_run_generate_chapter_unit",
    "_EXAM_CHAPTER_RE",
    "_exam_chapter_ids",
    "_chapter_exam_pool",
    "_generate_chapter_exam",
    "collect_generate_fanout_node",
    "_issue_chapter_id",
]
