from __future__ import annotations

import json

import pytest

from bookwiki.agents.document import model_to_document, parse_frontmatter_document
from bookwiki.agents.llm import generate_document_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import _repair_json_escapes
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import Citation
from tests.fakes import RecordingRuntime


def _document(*, ref_id: str = "src-p001", body: str = r"Body with $\mu$.") -> str:
    return f"""---
title: Point Estimation
concepts:
  - '$\\bar{{X}}$'
citations:
  - ref_id: {ref_id}
    quote: '$\\bar{{X}}$'
---
{body}"""


def test_parse_frontmatter_document_preserves_latex_backslashes() -> None:
    result = parse_frontmatter_document(
        _document(),
        output_model=ChapterResult,
        body_field="body_md",
        defaults={"chapter_id": "chapter-1", "owner_task_id": "chapter-1:chapter"},
        context={"allowed_citation_refs": {"src-p001"}},
    )

    assert isinstance(result, ChapterResult)
    assert result.concepts == [r"$\bar{X}$"]
    assert result.citations == [Citation(ref_id="src-p001", quote=r"$\bar{X}$")]
    assert result.body_md == r"Body with $\mu$."


def test_parse_frontmatter_document_missing_required_field_raises() -> None:
    text = """---
concepts: []
citations: []
---
Body"""

    with pytest.raises(ValueError, match="document validation failed"):
        parse_frontmatter_document(
            text,
            output_model=ChapterResult,
            body_field="body_md",
            defaults={"chapter_id": "chapter-1", "owner_task_id": "chapter-1:chapter"},
        )


def test_parse_frontmatter_document_threads_validation_context() -> None:
    with pytest.raises(ValueError, match="allowed source_refs"):
        parse_frontmatter_document(
            _document(ref_id="unknown"),
            output_model=ChapterResult,
            body_field="body_md",
            defaults={"chapter_id": "chapter-1", "owner_task_id": "chapter-1:chapter"},
            context={"allowed_citation_refs": {"src-p001"}},
        )


def test_repair_json_escapes_repairs_only_invalid_escapes() -> None:
    broken = r'{"x":"$\mu$"}'
    repaired = _repair_json_escapes(broken)

    assert json.loads(repaired) == {"x": r"$\mu$"}

    valid = r'{"x":"a\nb", "slash":"\\", "quote":"\""}'
    assert _repair_json_escapes(valid) == valid


def test_model_to_document_round_trips() -> None:
    model = ChapterResult(
        chapter_id="chapter-1",
        title="Point Estimation",
        body_md=r"Body with $\mu$.",
        concepts=[r"$\bar{X}$"],
        citations=[Citation(ref_id="src-p001", quote=r"$\bar{X}$")],
        owner_task_id="chapter-1:chapter",
    )

    parsed = parse_frontmatter_document(
        model_to_document(model, body_field="body_md"),
        output_model=ChapterResult,
        body_field="body_md",
        defaults={},
        context={"allowed_citation_refs": {"src-p001"}},
    )

    assert parsed == model


@pytest.mark.asyncio
async def test_generate_document_with_llm_retries_with_validation_error() -> None:
    runtime = RecordingRuntime([
        "---\nconcepts: []\ncitations: []\n---\nMissing title.",
        _document(body=r"Fixed body with $\mu$."),
    ])
    draft = ChapterResult(
        chapter_id="chapter-1",
        title="Draft",
        body_md="Draft body.",
        concepts=[],
        citations=[],
        owner_task_id="chapter-1:chapter",
    )

    result = await generate_document_with_llm(
        runtime=runtime,
        model="deepseek-v4-pro",
        output_model=ChapterResult,
        agent_name="TestAgent",
        prompt_name="test",
        prompt_template=PromptTemplate(body="Write the chapter."),
        inp={"chapter_id": "chapter-1"},
        draft=draft,
        body_field="body_md",
        defaults={"chapter_id": "chapter-1", "owner_task_id": "chapter-1:chapter"},
        allowed_citation_refs={"src-p001"},
        max_attempts=2,
    )

    assert result.body_md == r"Fixed body with $\mu$."
    assert len(runtime.calls) == 2
    assert "document validation failed" in runtime.calls[1]["user"]


def test_compact_input_truncates_when_over_token_budget(caplog) -> None:
    import logging

    from bookwiki.agents.llm import compact_input

    long_value = "x" * 50_000
    with caplog.at_level(logging.WARNING, logger="bookwiki.agents.llm"):
        result = compact_input(long_value, model="deepseek-v4-pro", max_tokens=100)

    assert result.endswith("[truncated]")
    assert len(result) < len(long_value)
    messages = [record.getMessage() for record in caplog.records]
    assert any("truncated" in msg and "model=deepseek-v4-pro" in msg for msg in messages)


def test_compact_input_no_warning_within_budget(caplog) -> None:
    import logging

    from bookwiki.agents.llm import compact_input

    with caplog.at_level(logging.WARNING, logger="bookwiki.agents.llm"):
        result = compact_input("x" * 1_000, model="deepseek-v4-pro")

    assert result == "x" * 1_000
    assert caplog.records == []


def test_compact_input_recurses_into_mappings_and_lists() -> None:
    from bookwiki.agents.llm import compact_input

    out = compact_input(
        {"big": "y" * 5_000, "nested": [{"small": "z" * 10}]},
        model="deepseek-v4-pro",
        max_tokens=50,
    )

    assert out["big"].endswith("[truncated]")
    assert out["nested"][0]["small"] == "z" * 10


def test_compact_input_keeps_large_real_world_field_within_model_budget() -> None:
    from bookwiki.agents.llm import compact_input

    # The largest field observed in real runs was ~91k chars. The model-aware
    # budget must keep it intact rather than re-introducing the old 40k-char cap.
    field = "数据" * 45_000  # 90k chars
    result = compact_input(field, model="deepseek-v4-pro")

    assert result == field
