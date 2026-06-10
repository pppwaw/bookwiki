from __future__ import annotations

import pytest

from bookwiki.agents.section_agent import SectionAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "<!-- source_ref: src-p001 -->\nPoint estimation content."


@pytest.mark.asyncio
async def test_section_agent_carries_knowledge_questions_and_application_requests() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "section_index": 0,
                "title": "Point Estimation",
                "body_md": "Point estimation chooses one value for a parameter.",
                "concepts": ["point estimation"],
                "citations": [{"ref_id": "src-p001", "quote": "Point estimation"}],
                "figure_requests": [],
                "knowledge_questions": [
                    {
                        "question": "What does point estimation return?",
                        "choices": ["One value", "A full textbook"],
                        "answer": "One value",
                        "explanation": "The section defines it as choosing one value.",
                        "citations": [{"ref_id": "src-p001", "quote": "Point estimation"}],
                    }
                ],
                "application_question_requests": [
                    {
                        "topic": "sample mean as point estimate",
                        "concept": "point estimation",
                        "rationale": "The section introduces estimating a parameter from data.",
                        "source_refs": ["src-p001"],
                    }
                ],
                "owner_task_id": "chapter-1:section:000",
            }
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

    assert result.knowledge_questions[0].answer == "One value"
    assert result.application_question_requests[0].topic == "sample mean as point estimate"


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
