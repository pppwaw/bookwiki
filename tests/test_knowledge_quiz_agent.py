from __future__ import annotations

import pytest
from pydantic import ValidationError

from bookwiki.agents.knowledge_quiz_agent import KnowledgeQuizAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.quiz import KnowledgeQuizResult
from tests.fakes import RecordingRuntime


def _input_payload() -> dict[str, object]:
    return {
        "chapter_id": "chapter-1",
        "section_index": 0,
        "title": "抽样分布",
        "body_md": "本段定义统计量与样本均值 $\\bar{X}$。",
        "concepts": ["统计量"],
        "allowed_source_refs": ["src-p001"],
        "language": "zh-CN",
        "book_notes": "",
    }


@pytest.mark.asyncio
async def test_knowledge_quiz_agent_returns_schema_guided_items() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "section_index": 0,
                "items": [
                    {
                        "question": "以下哪一项是本段定义的统计量？",
                        "choices": ["$\\bar{X}$", "$\\mu$"],
                        "answer": "$\\bar{X}$",
                        "explanation": "样本均值由样本计算得到，因此是统计量。",
                        "citations": [{"ref_id": "src-p001", "quote": "统计量"}],
                    }
                ],
                "owner_task_id": "chapter-1:section:000:knowledge_quiz",
            }
        ]
    )

    result = await KnowledgeQuizAgent().run(
        _input_payload(), model="deepseek-v4-flash", runtime=runtime
    )

    assert isinstance(result, KnowledgeQuizResult)
    assert result.items[0].choices == ["$\\bar{X}$", "$\\mu$"]
    assert runtime.calls[0]["output_model"] is KnowledgeQuizResult
    assert runtime.calls[0]["context"] == {"allowed_citation_refs": {"src-p001"}}


@pytest.mark.asyncio
async def test_knowledge_quiz_agent_rejects_disallowed_citations() -> None:
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "section_index": 0,
                "items": [
                    {
                        "question": "以下哪一项是本段定义的统计量？",
                        "choices": ["$\\bar{X}$", "$\\mu$"],
                        "answer": "$\\bar{X}$",
                        "explanation": "样本均值由样本计算得到，因此是统计量。",
                        "citations": [{"ref_id": "src-p999", "quote": "统计量"}],
                    }
                ],
                "owner_task_id": "chapter-1:section:000:knowledge_quiz",
            },
            {
                "chapter_id": "chapter-1",
                "section_index": 0,
                "items": [
                    {
                        "question": "以下哪一项是本段定义的统计量？",
                        "choices": ["$\\bar{X}$", "$\\mu$"],
                        "answer": "$\\bar{X}$",
                        "explanation": "样本均值由样本计算得到，因此是统计量。",
                        "citations": [{"ref_id": "src-p999", "quote": "统计量"}],
                    }
                ],
                "owner_task_id": "chapter-1:section:000:knowledge_quiz",
            },
        ]
    )

    with pytest.raises(ValidationError):
        await KnowledgeQuizAgent().run(_input_payload(), model="deepseek-v4-flash", runtime=runtime)


@pytest.mark.asyncio
async def test_knowledge_quiz_agent_offline_echoes_empty_items() -> None:
    result = await KnowledgeQuizAgent().run(
        _input_payload(), model="deepseek-v4-flash", runtime=TestLLMRuntime()
    )

    assert result.items == []
    assert result.owner_task_id == "chapter-1:section:000:knowledge_quiz"
