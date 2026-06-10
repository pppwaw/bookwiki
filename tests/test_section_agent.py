from __future__ import annotations

import pytest

from bookwiki.agents.section_agent import SectionAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "<!-- source_ref: src-p001 -->\nPoint estimation content."


@pytest.mark.asyncio
async def test_section_agent_frontmatter_omits_knowledge_questions() -> None:
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
application_question_requests:
  - topic: sample mean as point estimate
    concept: point estimation
    rationale: The section introduces estimating a parameter from data.
    source_refs:
      - src-p001
---
Point estimation chooses one value for a parameter with $\\mu$ preserved."""
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

    assert result.knowledge_questions == []
    assert result.application_question_requests[0].topic == "sample mean as point estimate"
    assert r"$\mu$" in result.body_md
    assert "knowledge_questions" not in SectionAgent.prompt_template.body


@pytest.mark.asyncio
async def test_section_agent_offline_echoes_empty_quiz_fields() -> None:
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

    assert result.knowledge_questions == []
    assert result.application_question_requests == []
