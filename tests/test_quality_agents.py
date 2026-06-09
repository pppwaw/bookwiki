from __future__ import annotations

import pytest

from bookwiki.agents.chapter_content_rewrite_agent import ChapterContentRewriteAgent
from bookwiki.agents.concept_content_rewrite_agent import ConceptContentRewriteAgent
from bookwiki.agents.quality_check_agent import QualityCheckAgent
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.concept import ConceptResult
from tests.fakes import RecordingRuntime

LEAK_BODY = "# Z 检验\n\n随后查得select the cutoff value to control the error rate。"


@pytest.mark.asyncio
async def test_quality_check_agent_reports_language_leak_quote() -> None:
    quote = "查得select the cutoff value to control"
    runtime = RecordingRuntime(
        [
            {
                "owner_task_id": "concept-quality:Z-test-z检验",
                "findings": [
                    {
                        "category": "language_leak",
                        "quote": quote,
                        "explanation": "中文后直接粘连未翻译英文片段。",
                    }
                ],
            }
        ]
    )

    result = await QualityCheckAgent().run(
        {
            "owner_task_id": "concept-quality:Z-test-z检验",
            "title": "Z-test z检验",
            "body_md": LEAK_BODY,
            "language": "zh-CN",
            "kind": "concept",
        },
        model="deepseek-v4-flash",
        runtime=runtime,
    )

    assert len(result.findings) == 1
    assert result.findings[0].category == "language_leak"
    assert result.findings[0].quote in LEAK_BODY
    prompt = runtime.calls[0]["user"]
    assert "select the cutoff value" in prompt
    assert "置信区间 (Confidence Interval, CI)" in prompt


@pytest.mark.asyncio
async def test_quality_check_agent_echoes_empty_draft_for_clean_or_legitimate_english() -> None:
    body = (
        "置信区间 (Confidence Interval, CI) 是常见术语。\n\n"
        "公式 $select the cutoff value$ 不应作为正文漏译。\n\n"
        "引用中保留英文原句: Smith writes, 'select the cutoff value'."
    )

    result = await QualityCheckAgent().run(
        {
            "owner_task_id": "chapter-1:chapter",
            "title": "置信区间",
            "body_md": body,
            "language": "zh-CN",
            "kind": "chapter",
        },
        model="stub",
        runtime=TestLLMRuntime(),
    )

    assert result.findings == []


@pytest.mark.asyncio
async def test_chapter_content_rewrite_agent_preserves_identifiers_and_citations() -> None:
    fixed = {
        "chapter_id": "chapter-1",
        "title": "Z 检验",
        "body_md": "# Z 检验\n\n随后查得用于控制错误率的临界值。\n\n<BookFigure id=\"fig-1\" />",
        "concepts": ["Z-test"],
        "citations": [{"ref_id": "source-p001", "quote": "select cutoff"}],
        "owner_task_id": "chapter-1:chapter",
    }
    runtime = RecordingRuntime([fixed])

    result = await ChapterContentRewriteAgent().run(
        {
            "chapter_id": "chapter-1",
            "title": "Z 检验",
            "body_md": (
                "# Z 检验\n\n随后查得select the cutoff value。\n\n"
                "<BookFigure id=\"fig-1\" />"
            ),
            "concepts": ["Z-test"],
            "citations": [{"ref_id": "source-p001", "quote": "select cutoff"}],
            "owner_task_id": "chapter-1:chapter",
            "quality_findings": [{"quote": "查得select the cutoff value", "explanation": "漏译"}],
            "language": "zh-CN",
            "book_notes": "",
            "allowed_source_refs": ["source-p001"],
        },
        model="deepseek-v4-pro",
        runtime=runtime,
    )

    assert isinstance(result, ChapterResult)
    assert result.chapter_id == "chapter-1"
    assert result.owner_task_id == "chapter-1:chapter"
    assert result.citations[0].ref_id == "source-p001"
    assert '<BookFigure id="fig-1" />' in result.body_md
    assert runtime.calls[0]["context"] == {"allowed_citation_refs": {"source-p001"}}


@pytest.mark.asyncio
async def test_concept_content_rewrite_agent_preserves_identifiers_and_citations() -> None:
    fixed = {
        "name": "Z-test z检验",
        "summary_md": "Z 检验概念。",
        "body_md": "随后查得用于控制错误率的临界值。",
        "related": ["显著性水平"],
        "citations": [{"ref_id": "source-p002", "quote": "select cutoff"}],
        "owner_task_id": "concept:Z-test z检验",
    }
    runtime = RecordingRuntime([fixed])

    result = await ConceptContentRewriteAgent().run(
        {
            "name": "Z-test z检验",
            "summary_md": "Z 检验概念。",
            "body_md": "随后查得select the cutoff value。",
            "related": ["显著性水平"],
            "citations": [{"ref_id": "source-p002", "quote": "select cutoff"}],
            "owner_task_id": "concept:Z-test z检验",
            "quality_findings": [{"quote": "查得select the cutoff value", "explanation": "漏译"}],
            "language": "zh-CN",
            "book_notes": "",
            "allowed_source_refs": ["source-p002"],
        },
        model="deepseek-v4-pro",
        runtime=runtime,
    )

    assert isinstance(result, ConceptResult)
    assert result.name == "Z-test z检验"
    assert result.owner_task_id == "concept:Z-test z检验"
    assert result.citations[0].ref_id == "source-p002"
