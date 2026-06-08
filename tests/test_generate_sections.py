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
