"""Unit tests for the agentic chapter-section pipeline (Phase 3).

Covers ``generate_chapter_sections``: section assembly order, stable owner ids,
chapter-level quiz/card generation, caching, and the repair-exhaustion fallback
that records a warning ``Issue`` and keeps the imperfect section.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import bookwiki.generate.sections as sections
from bookwiki.generate.sections import generate_chapter_sections
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "# Search\n\n<!-- source_ref: src-p001 -->\n\nState space search content."


def _cfg(book_dir: Path, runtime: Any) -> BookConfig:
    return BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=runtime,
        generation={"quizPerChapter": 2, "cardsPerChapter": 2},
    )


@pytest.mark.asyncio
async def test_generate_chapter_sections_assembles_ordered_body(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "book", TestLLMRuntime())

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["Frontier", "Heuristics"],
        figures=[],
        skeleton_payload={},
    )

    body = result.chapter.body_md
    assert body.startswith("# Search")
    assert "## Frontier" in body
    assert "## Heuristics" in body
    # Sections are assembled in index order.
    assert body.index("## Frontier") < body.index("## Heuristics")
    assert result.chapter.owner_task_id == "chapter-1:chapter"
    assert result.quiz.owner_task_id == "chapter-1:quiz"
    assert result.card.owner_task_id == "chapter-1:card"
    assert result.summary.chapter_id == "chapter-1"
    assert result.issues == []


@pytest.mark.asyncio
async def test_generate_chapter_sections_is_cacheable(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "book", TestLLMRuntime())
    kwargs: dict[str, Any] = {
        "cfg": cfg,
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": SOURCE_MD,
        "source_path": "work/chapter_sources/chapter-1/source.md",
        "topics": ["Frontier"],
        "figures": [],
        "skeleton_payload": {},
    }

    first = await generate_chapter_sections(**kwargs)
    second = await generate_chapter_sections(**kwargs)

    assert first.cache_hit is False
    assert second.cache_hit is True


def _plan_response(concepts: list[str]) -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "sections": [
            {
                "chapter_id": "chapter-1",
                "index": 0,
                "title": "S0",
                "topics_covered": ["t0"],
                "concepts_introduced": concepts,
                "learning_goal": "goal",
            }
        ],
        "owner_task_id": "chapter-1:section_plan",
    }


def _section_response(concepts: list[str]) -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "section_index": 0,
        "title": "S0",
        "body_md": "Section body about the concept.",
        "concepts": concepts,
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
        "figure_requests": [],
        "owner_task_id": "chapter-1:section:000",
    }


def _section_response_with_body(body_md: str) -> dict[str, Any]:
    payload = _section_response([])
    payload["body_md"] = body_md
    payload["concepts"] = []
    return payload


def _application_quiz_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    """One ApplicationQuizAgent call now returns a single QuizItem (per-slot contract)."""
    return items[0]


def _card_response() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "items": [],
        "owner_task_id": "chapter-1:card",
    }


def _summary_response() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "summary_md": "Summary.",
        "key_points": ["point"],
        "citations": [],
        "owner_task_id": "chapter-1:summary",
    }


@pytest.mark.asyncio
async def test_generate_chapter_sections_records_fallback_warning(tmp_path: Path) -> None:
    # The section keeps redefining a concept owned by another chapter; both
    # repair rounds fail, so the chapter still completes but logs a warning.
    skeleton_payload = {
        "chapter_uses": [
            {"canonical": "Owned Concept", "aliases": [], "first_chapter_id": "chapter-2"}
        ],
        "chapter_owns": [],
        "alias_map": {},
    }
    runtime = RecordingRuntime(
        [
            _plan_response(["Owned Concept"]),
            _section_response(["Owned Concept"]),  # initial: violates ownership
            _section_response(["Owned Concept"]),  # repair response reused by cache in round 2
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = _cfg(tmp_path / "book", runtime)

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0"],
        figures=[],
        skeleton_payload=skeleton_payload,
    )

    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.severity == "warning"
    assert issue.code == "SECTION_VALIDATION_UNRESOLVED"
    assert issue.owner_task_id == "chapter-1:chapter"
    # The imperfect section is still assembled into the chapter body.
    assert "## S0" in result.chapter.body_md
    # All scripted responses after the plan were consumed.
    assert runtime.responses == []


@pytest.mark.asyncio
async def test_generate_chapter_sections_inline_repairs_bare_mdx_math(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response([]),
            _section_response_with_body("当 n<30 时使用 t 分布。"),
            {"status": "fixed", "notes": "wrapped the bare comparison in math"},
            _card_response(),
            _summary_response(),
        ],
        tool_calls=[("str_replace", {"old_str": "n<30", "new_str": "$n < 30$"})],
    )
    cfg = _cfg(tmp_path / "book", runtime)

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0"],
        figures=[],
        skeleton_payload={},
    )

    assert "$n < 30$" in result.chapter.body_md
    assert "n<30" not in result.chapter.body_md
    assert result.issues == []
    assert "ChapterMdxEditRepairAgent" in runtime.calls[2]["user"]


@pytest.mark.asyncio
async def test_generate_chapter_sections_inline_exhaustion_warns_and_completes(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response([]),
            _section_response_with_body("当 n<30 时使用 t 分布。"),
            # Two repair rounds where the edit loop makes no effective edits.
            {"status": "gave_up", "notes": "could not isolate the breakage"},
            {"status": "gave_up", "notes": "could not isolate the breakage"},
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = _cfg(tmp_path / "book", runtime)
    cfg.generation["maxRepairRounds"] = 2

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0"],
        figures=[],
        skeleton_payload={},
    )

    assert result.chapter.body_md.endswith("当 n<30 时使用 t 分布。")
    issue = next(issue for issue in result.issues if issue.code == "CHAPTER_VALIDATION_UNRESOLVED")
    assert issue.severity == "warning"
    assert issue.owner_task_id == "chapter-1:chapter"
    assert result.quiz.owner_task_id == "chapter-1:quiz"


def _two_section_plan() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "sections": [
            {
                "chapter_id": "chapter-1",
                "index": 0,
                "title": "Foundations",
                "topics_covered": ["t0"],
                "concepts_introduced": [],
                "learning_goal": "lay groundwork",
            },
            {
                "chapter_id": "chapter-1",
                "index": 1,
                "title": "Estimators",
                "topics_covered": ["t1"],
                "concepts_introduced": [],
                "learning_goal": "build estimators",
            },
        ],
        "owner_task_id": "chapter-1:section_plan",
    }


def _section_response_at(index: int, title: str) -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "section_index": index,
        "title": title,
        "body_md": "Section body about the concept.",
        "concepts": [],
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
        "figure_requests": [],
        "owner_task_id": f"chapter-1:section:{index:03d}",
    }


def _section_response_with_slot(index: int, title: str) -> dict[str, Any]:
    payload = _section_response_at(index, title)
    payload["body_md"] = (
        "Section body about the concept.\n\n"
        '<QuizBlock>\n<QuizItemSlot id="auto" topic="application scenario" '
        f'concept="{title}" sourceRefs={{["src-p001"]}} />\n</QuizBlock>'
    )
    return payload


def _application_item(index: int, question: str | None = None) -> dict[str, Any]:
    return {
        "question": question or f"Scenario ${index}+1$ asks for a conclusion.",
        "choices": [f"${index + 1}$", f"${index + 2}$"],
        "answer": f"${index + 1}$",
        "explanation": f"Compute ${index}+1={index + 1}$, so the first option is correct.",
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
    }


@pytest.mark.asyncio
async def test_section_and_summary_receive_chapter_outline_and_position(tmp_path: Path) -> None:
    # Each section must see the whole chapter's outline (so a later same-chapter
    # topic is not mistaken for "the next chapter") plus its own position flags.
    runtime = RecordingRuntime(
        [
            _two_section_plan(),
            _section_response_at(0, "Foundations"),
            _section_response_at(1, "Estimators"),
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = _cfg(tmp_path / "book", runtime)

    await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0", "t1"],
        figures=[],
        skeleton_payload={},
    )

    # calls: [plan, section-0, section-1, card, summary]
    section0 = runtime.calls[1]["user"]
    section1 = runtime.calls[2]["user"]
    summary = runtime.calls[4]["user"]

    # Section 0 can see the later section's title only via the injected outline.
    assert '"chapter_outline"' in section0
    assert "Estimators" in section0
    # Position flags: only the final section is is_last.
    assert '"is_first": true' in section0
    assert '"is_last": false' in section0
    assert '"is_last": true' in section1
    # The summary is scoped by the same outline.
    assert '"chapter_outline"' in summary


@pytest.mark.asyncio
async def test_generate_chapter_sections_fills_application_slots_from_sections(
    tmp_path: Path,
) -> None:
    # Two sections each author an application <QuizItemSlot/>; the application quiz agent
    # fills both, and each item is bound to its section's canonical slot id by order.
    runtime = RecordingRuntime(
        [
            _two_section_plan(),
            _section_response_with_slot(0, "Foundations"),
            _section_response_with_slot(1, "Estimators"),
            _application_quiz_response([_application_item(1)]),
            _application_quiz_response([_application_item(2)]),
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = _cfg(tmp_path / "book", runtime)

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0", "t1"],
        figures=[],
        skeleton_payload={},
    )

    assert [item.answer for item in result.quiz.items] == ["$2$", "$3$"]
    # Each filled item carries its section's canonical slot id (bound by order, not by the LLM).
    assert [item.slot_id for item in result.quiz.items] == [
        "chapter-1:s0:slot-000",
        "chapter-1:s1:slot-000",
    ]
    # Only the application quiz agent runs now (no separate knowledge agent).
    assert runtime.calls[3]["output_model"].__name__ == "QuizItem"
    assert '"request"' in runtime.calls[3]["user"]


@pytest.mark.asyncio
async def test_generate_chapter_sections_repairs_invalid_application_quiz_mdx(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response([]),
            _section_response_with_slot(0, "S0"),
            _application_quiz_response([_application_item(1, question="When n<30, what follows?")]),
            _application_quiz_response(
                [_application_item(1, question="When $n < 30$, what follows?")]
            ),
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = _cfg(tmp_path / "book", runtime)

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0"],
        figures=[],
        skeleton_payload={},
    )

    assert "When $n < 30$" in result.quiz.items[0].question
    assert result.issues == []
    assert runtime.calls[3]["output_model"].__name__ == "QuizItem"
    assert "mdx_errors" in runtime.calls[3]["user"]


@pytest.mark.asyncio
async def test_generate_chapter_sections_warns_when_application_quiz_mdx_stays_broken(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response([]),
            _section_response_with_slot(0, "S0"),
            _application_quiz_response([_application_item(1, question="When n<30, what follows?")]),
            _application_quiz_response([_application_item(1, question="When n<30, what follows?")]),
            _application_quiz_response([_application_item(1, question="When n<30, what follows?")]),
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = _cfg(tmp_path / "book", runtime)
    cfg.generation["maxRepairRounds"] = 2

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0"],
        figures=[],
        skeleton_payload={},
    )

    issue = next(issue for issue in result.issues if issue.code == "QUIZ_VALIDATION_UNRESOLVED")
    assert issue.severity == "warning"
    assert issue.owner_task_id == "chapter-1:quiz"
    assert result.quiz.items[0].question == "When n<30, what follows?"
    assert result.summary.chapter_id == "chapter-1"


@pytest.mark.asyncio
async def test_sections_fan_out_bounded_by_section_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = BookConfig(
        book_dir=tmp_path / "book",
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
        generation={"quizPerChapter": 2, "cardsPerChapter": 2, "maxSectionConcurrency": 2},
    )

    active = 0
    peak = 0
    original = sections._generate_validated_section

    async def tracking_section(**kwargs: Any):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            # Yield control so siblings can overlap before this one finishes.
            await asyncio.sleep(0.02)
            return await original(**kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(sections, "_generate_validated_section", tracking_section)

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0", "t1", "t2"],
        figures=[],
        skeleton_payload={},
    )

    # 3 sections, semaphore=2: sections ran in parallel (peak > 1) but never exceeded
    # the configured bound.
    assert peak == 2
    # Order is still preserved by gather, so the assembled body keeps section order.
    body = result.chapter.body_md
    assert body.index("## t0") < body.index("## t1") < body.index("## t2")


def test_section_position_flags_boundaries_with_nonzero_base() -> None:
    from bookwiki.generate.sections import _section_position
    from bookwiki.schemas.section import SectionSpec

    outline = [{"index": 1}, {"index": 2}, {"index": 3}]

    def spec(index: int) -> SectionSpec:
        return SectionSpec(
            chapter_id="chapter-1",
            index=index,
            title=f"s{index}",
            learning_goal="g",
            topics_covered=[],
            concepts_introduced=[],
        )

    first = _section_position(spec(1), outline)
    middle = _section_position(spec(2), outline)
    last = _section_position(spec(3), outline)

    assert first["is_first"] is True and first["is_last"] is False
    assert middle["is_first"] is False and middle["is_last"] is False
    assert last["is_first"] is False and last["is_last"] is True


def test_section_position_zero_based_still_flags_first() -> None:
    from bookwiki.generate.sections import _section_position
    from bookwiki.schemas.section import SectionSpec

    outline = [{"index": 0}, {"index": 1}]
    spec0 = SectionSpec(
        chapter_id="chapter-1",
        index=0,
        title="s0",
        learning_goal="g",
        topics_covered=[],
        concepts_introduced=[],
    )

    assert _section_position(spec0, outline)["is_first"] is True
