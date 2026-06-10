from __future__ import annotations

import pytest

from bookwiki.agents.application_quiz_agent import ApplicationQuizAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "<!-- source_ref: src-p001 -->\nA sample of size n=31 estimates a mean."


@pytest.mark.asyncio
async def test_application_quiz_agent_returns_requested_application_items() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "items": [
                    {
                        "question": "A study has $n=31$ observations. Which method is appropriate?",
                        "choices": ["Use the large-sample approximation", "Treat $n$ as $3$"],
                        "answer": "Use the large-sample approximation",
                        "explanation": "Because $n=31$ exceeds $30$, the approximation applies.",
                        "citations": [{"ref_id": "src-p001", "quote": "n=31"}],
                    }
                ],
                "placements": [],
                "owner_task_id": "chapter-1:quiz",
            }
        ]
    )

    result = await ApplicationQuizAgent().run(
        {
            "chapter_id": "chapter-1",
            "title": "Point Estimation",
            "source_md": SOURCE_MD,
            "chapter_body_md": "# Point Estimation\n\nUse sample size rules.",
            "requests": [
                {
                    "topic": "sample size threshold",
                    "concept": "large-sample approximation",
                    "rationale": "The section teaches the threshold.",
                    "source_refs": ["src-p001"],
                }
            ],
            "allowed_source_refs": ["src-p001"],
        },
        model="deepseek-v4-pro",
        runtime=runtime,
    )

    assert "$n=31$" in result.items[0].question
    assert result.items[0].citations[0].ref_id == "src-p001"
    assert runtime.calls[0]["context"] == {"allowed_citation_refs": {"src-p001"}}


@pytest.mark.asyncio
async def test_application_quiz_agent_offline_echoes_empty_items() -> None:
    result = await ApplicationQuizAgent().run(
        {
            "chapter_id": "chapter-1",
            "title": "Point Estimation",
            "source_md": SOURCE_MD,
            "chapter_body_md": "# Point Estimation\n\nUse sample size rules.",
            "requests": [],
            "allowed_source_refs": ["src-p001"],
        },
        model="deepseek-v4-pro",
        runtime=TestLLMRuntime(),
    )

    assert result.items == []
