"""Unit tests for ``SectionPlannerAgent`` (Phase 3).

The planner splits a chapter into teaching units. Its deterministic draft (one
section per curated topic, floored at one section) is what ``TestLLMRuntime``
echoes back offline, so these tests assert the draft contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from bookwiki.agents.section_planner_agent import SectionPlannerAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.section import SectionPlan


def _payload(chapter_id: str, title: str, topics: list[str]) -> dict[str, Any]:
    return {
        "chapter_id": chapter_id,
        "title": title,
        "topics": topics,
        "source_md": "# Source\n<!-- source_ref: src-p001 -->\nbody",
        "language": "zh-CN",
        "book_notes": "",
    }


@pytest.mark.asyncio
async def test_planner_drafts_one_section_per_topic() -> None:
    result = await SectionPlannerAgent().run(
        _payload("chapter-1", "Search", ["State space search", "Heuristics"]),
        model="stub",
        runtime=TestLLMRuntime(),
    )

    assert isinstance(result, SectionPlan)
    assert [section.index for section in result.sections] == [0, 1]
    assert [section.title for section in result.sections] == ["State space search", "Heuristics"]
    assert result.sections[0].topics_covered == ["State space search"]


@pytest.mark.asyncio
async def test_planner_floors_at_one_section_when_no_topics() -> None:
    result = await SectionPlannerAgent().run(
        _payload("chapter-3", "Overview", []),
        model="stub",
        runtime=TestLLMRuntime(),
    )

    assert len(result.sections) == 1
    assert result.sections[0].index == 0
    assert result.sections[0].title == "Overview"
    assert result.sections[0].topics_covered == []


@pytest.mark.asyncio
async def test_planner_owner_task_id_suffix() -> None:
    result = await SectionPlannerAgent().run(
        _payload("chapter-2", "Deep", ["t1"]),
        model="stub",
        runtime=TestLLMRuntime(),
    )

    assert result.chapter_id == "chapter-2"
    assert result.owner_task_id.endswith(":section_plan")
    # Section count never exceeds the topic count.
    assert len(result.sections) <= max(1, 1)
