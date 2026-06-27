from __future__ import annotations

import pytest

from bookwiki.agents.exam_agent import ExamAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "<!-- source_ref: src-p001 -->\nA* search expands by f(n) = g(n) + h(n)."

INP = {
    "chapter_id": "chapter-1",
    "title": "Heuristic Search",
    "source_md": SOURCE_MD,
    "chapter_body_md": "# Heuristic Search\n\nA* expands by f = g + h.",
    "allowed_source_refs": ["src-p001"],
    "exam_pool": [
        {"question": "Past paper: state the A* evaluation function.", "source_refs": ["src-p001"]}
    ],
}


@pytest.mark.asyncio
async def test_exam_agent_returns_mixed_paper() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "owner_task_id": "ignored-by-agent",
                "questions": [
                    {
                        "type": "single_choice",
                        "id": "exam-1",
                        "question": "What does A* minimise?",
                        "options": ["$f(n)$", "$g(n)$ only"],
                        "answer": ["$f(n)$"],
                        "explanation": "A* expands by $f = g + h$.",
                        "from_exam": True,
                        "source_refs": ["src-p001"],
                    },
                    {
                        "type": "fill_blank",
                        "id": "exam-2",
                        "question": "A* uses $f(n) = $ ___ $+$ ___.",
                        "accepted_answers": [["$g(n)$"], ["$h(n)$"]],
                        "source_refs": ["src-p001"],
                    },
                    {
                        "type": "worked",
                        "id": "exam-3",
                        "question": "Prove A* is optimal with an admissible heuristic.",
                        "reference_answer": "Assume an admissible $h$; then ...",
                        "rubric": [{"point": "states admissibility", "weight": 1.0}],
                        "source_refs": ["src-p001"],
                    },
                ],
            }
        ]
    )

    result = await ExamAgent().run(INP, model="deepseek-v4-pro", runtime=runtime)

    assert [question.type for question in result.questions] == [
        "single_choice",
        "fill_blank",
        "worked",
    ]
    assert result.questions[0].from_exam is True
    # The agent owns the task id regardless of what the model returned.
    assert result.owner_task_id == "chapter-1:exam"
    assert result.chapter_id == "chapter-1"


@pytest.mark.asyncio
async def test_exam_agent_offline_returns_draft_paper() -> None:
    result = await ExamAgent().run(INP, model="deepseek-v4-pro", runtime=TestLLMRuntime())

    assert result.owner_task_id == "chapter-1:exam"
    assert len(result.questions) >= 1
