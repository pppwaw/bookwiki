from __future__ import annotations

import pytest

from bookwiki.agents.section_agent import SectionAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "<!-- source_ref: src-p001 -->\nPoint estimation content."


@pytest.mark.asyncio
async def test_section_agent_authors_quizzes_inline_not_in_frontmatter() -> None:
    runtime = RecordingRuntime(
        [
            """---
section_index: 0
title: Point Estimation
concepts:
  - point estimation
citations:
  - ref_id: src-p001
    quote: Point estimation
figure_requests: []
---
Point estimation chooses one value for a parameter with $\\mu$ preserved.

<QuizItemSlot id="auto" topic="sample mean" sourceRefs={["src-p001"]} />"""
        ]
    )

    result = await SectionAgent().run(
        {
            "chapter_id": "chapter-1",
            "title": "Point Estimation",
            "source_md": SOURCE_MD,
            "section": {"index": 0, "title": "Point Estimation"},
        },
        model="deepseek-v4-pro",
        runtime=runtime,
    )

    # Quizzes are authored inline in body_md, never carried as frontmatter fields.
    assert r"$\mu$" in result.body_md
    assert "<QuizItemSlot" in result.body_md
    assert "application_question_requests" not in SectionAgent.prompt_template.body
    assert "knowledge_questions" not in SectionAgent.prompt_template.body


@pytest.mark.asyncio
async def test_section_agent_offline_produces_body() -> None:
    result = await SectionAgent().run(
        {
            "chapter_id": "chapter-1",
            "title": "Point Estimation",
            "source_md": SOURCE_MD,
            "section": {"index": 0, "title": "Point Estimation"},
        },
        model="deepseek-v4-pro",
        runtime=TestLLMRuntime(),
    )

    assert result.chapter_id == "chapter-1"
    assert result.section_index == 0
    assert isinstance(result.body_md, str)
