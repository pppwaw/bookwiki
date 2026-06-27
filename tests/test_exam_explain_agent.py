from __future__ import annotations

import pytest

from bookwiki.agents.exam_explain_agent import ExamExplainAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime

SOURCE_MD = "<!-- source_ref: exam-p001 -->\n1. Compute the gradient of f(x,y)=x^2+y^2."

INP = {
    "chapter_id": "Mid-Term-Exam",
    "title": "Mid-Term Exam",
    "source_md": SOURCE_MD,
    "chapter_body_md": "# Mid-Term Exam\n\n1. Compute the gradient of $f(x,y)=x^2+y^2$.",
    "allowed_source_refs": ["exam-p001"],
}


@pytest.mark.asyncio
async def test_explain_agent_returns_walkthrough_with_recap() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "Mid-Term-Exam",
                "owner_task_id": "ignored-by-agent",
                "questions": [
                    {
                        "type": "worked",
                        "id": "q1",
                        "question": "Compute the gradient of $f(x,y)=x^2+y^2$.",
                        "reference_answer": "$\\nabla f = (2x, 2y)$.",
                        "rubric": [{"point": "partial derivatives", "weight": 1.0}],
                        "concept_recap_md": "梯度是各偏导组成的向量 $\\nabla f$。",
                        "source_refs": ["exam-p001"],
                    }
                ],
            }
        ]
    )

    result = await ExamExplainAgent().run(INP, model="deepseek-v4-pro", runtime=runtime)

    assert result.owner_task_id == "Mid-Term-Exam:explain"
    assert result.questions[0].concept_recap_md.startswith("梯度")


@pytest.mark.asyncio
async def test_explain_agent_offline_returns_draft() -> None:
    result = await ExamExplainAgent().run(INP, model="deepseek-v4-pro", runtime=TestLLMRuntime())

    assert result.owner_task_id == "Mid-Term-Exam:explain"
    assert len(result.questions) >= 1
