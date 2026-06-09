"""Unit tests for the agentic chapter-section pipeline (Phase 3).

Covers ``generate_chapter_sections``: section assembly order, stable owner ids,
chapter-level quiz/card generation, caching, and the repair-exhaustion fallback
that records a warning ``Issue`` and keeps the imperfect section.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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


def _chapter_response_with_body(body_md: str) -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "title": "Search",
        "body_md": body_md,
        "concepts": [],
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
        "owner_task_id": "chapter-1:chapter",
    }


def _quiz_card_response() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "quiz": {
            "chapter_id": "chapter-1",
            "items": [],
            "placements": [],
            "owner_task_id": "chapter-1:quiz",
        },
        "card": {
            "chapter_id": "chapter-1",
            "items": [],
            "owner_task_id": "chapter-1:card",
        },
        "owner_task_id": "chapter-1:quizcard",
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
            _section_response(["Owned Concept"]),  # repair round 1: still bad
            _section_response(["Owned Concept"]),  # repair round 2: still bad
            _quiz_card_response(),
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
    # All four scripted responses after the plan were consumed (1 + 2 repairs).
    assert runtime.responses == []


@pytest.mark.asyncio
async def test_generate_chapter_sections_inline_repairs_bare_mdx_math(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response([]),
            _section_response_with_body("当 n<30 时使用 t 分布。"),
            _chapter_response_with_body("# Search\n\n## S0\n\n当 $n < 30$ 时使用 t 分布。"),
            _quiz_card_response(),
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

    assert "$n < 30$" in result.chapter.body_md
    assert "n<30" not in result.chapter.body_md
    assert result.issues == []
    assert runtime.calls[2]["output_model"].__name__ == "ChapterResult"


@pytest.mark.asyncio
async def test_generate_chapter_sections_inline_exhaustion_warns_and_completes(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response([]),
            _section_response_with_body("当 n<30 时使用 t 分布。"),
            _chapter_response_with_body("# Search\n\n## S0\n\n当 n<30 时使用 t 分布。"),
            _chapter_response_with_body("# Search\n\n## S0\n\n当 n<30 时使用 t 分布。"),
            _quiz_card_response(),
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


@pytest.mark.asyncio
async def test_section_and_summary_receive_chapter_outline_and_position(tmp_path: Path) -> None:
    # Each section must see the whole chapter's outline (so a later same-chapter
    # topic is not mistaken for "the next chapter") plus its own position flags.
    runtime = RecordingRuntime(
        [
            _two_section_plan(),
            _section_response_at(0, "Foundations"),
            _section_response_at(1, "Estimators"),
            _quiz_card_response(),
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

    # calls: [plan, section-0, section-1, quiz_card, summary]
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
