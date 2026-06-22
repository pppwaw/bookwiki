from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bookwiki.generate.validate_artifact import validate_artifact
from bookwiki.scheduler.config import BookConfig
from tests.fakes import RecordingRuntime


def _cfg(book_dir: Path, runtime: Any, *, quality_check: bool = False) -> BookConfig:
    return BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=runtime,
        generation={"qualityCheck": quality_check},
    )


@pytest.mark.asyncio
async def test_validate_artifact_flags_bare_mdx_math(tmp_path: Path) -> None:
    issues = await validate_artifact(
        body_md="# 章\n\n当 n<30 时使用 t 分布。",
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", RecordingRuntime([])),
    )

    assert any(issue.kind == "mdx" for issue in issues)


@pytest.mark.asyncio
async def test_validate_artifact_flags_quality_when_enabled(tmp_path: Path) -> None:
    runtime = RecordingRuntime(
        [
            {
                "owner_task_id": "chapter:inline-validation",
                "findings": [
                    {
                        "category": "language_leak",
                        "quote": "查得select the cutoff value",
                        "explanation": "中英粘连。",
                    }
                ],
            }
        ]
    )

    issues = await validate_artifact(
        body_md="随后查得select the cutoff value来控制错误率。",
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", runtime, quality_check=True),
    )

    quality = [issue for issue in issues if issue.kind == "quality"]
    assert quality[0].quote == "查得select the cutoff value"
    assert runtime.calls


@pytest.mark.asyncio
async def test_validate_artifact_quality_default_off_makes_zero_llm_calls(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime([])

    issues = await validate_artifact(
        body_md="随后查得select the cutoff value来控制错误率。",
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", runtime),
    )

    assert [issue for issue in issues if issue.kind == "quality"] == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_validate_artifact_flags_out_of_range_source_ref(tmp_path: Path) -> None:
    issues = await validate_artifact(
        body_md="正文。\n\n<!-- source_ref: src-p999 -->",
        kind="chapter",
        allowed_refs={"src-p001"},
        cfg=_cfg(tmp_path / "book", RecordingRuntime([])),
    )

    assert any(issue.kind == "citation" for issue in issues)


@pytest.mark.asyncio
async def test_validate_artifact_clean_chinese_returns_empty(tmp_path: Path) -> None:
    issues = await validate_artifact(
        body_md="# 章\n\n这是一段自然的中文讲解。",
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", RecordingRuntime([])),
    )

    assert issues == []


@pytest.mark.asyncio
async def test_validate_artifact_flags_source_meta_reference_with_quality_off(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime([])

    issues = await validate_artifact(
        body_md=(
            "若两个网络响应一致（源材料中对此有清晰的描述："
            "`N1 and N2 are equivalent`），则二者等效。"
        ),
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", runtime),
    )

    quality = [issue for issue in issues if issue.kind == "quality"]
    assert len(quality) == 1
    # The flagged span is a verbatim slice of body_md (so the rewrite agent can find it),
    # and it captures the whole leaking parenthetical, not just the trigger word.
    assert quality[0].quote == ("（源材料中对此有清晰的描述：`N1 and N2 are equivalent`）")
    # Deterministic detection makes no LLM call when qualityCheck is off.
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_validate_artifact_flags_source_text_reference(tmp_path: Path) -> None:
    issues = await validate_artifact(
        body_md="但根据实际参考方向，源文中最终得到的是 $-1$ V，叠加时注意符号。",
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", RecordingRuntime([])),
    )

    quality = [issue for issue in issues if issue.kind == "quality"]
    assert len(quality) == 1
    assert "源文中" in quality[0].quote


@pytest.mark.asyncio
async def test_validate_artifact_does_not_flag_circuit_source_terms(
    tmp_path: Path,
) -> None:
    issues = await validate_artifact(
        body_md=(
            "# 章\n\n电压源与电流源是基本元件；电源向电路供能，"
            "多个串联电压源可等效为一个源。资源文件不受影响。"
        ),
        kind="chapter",
        allowed_refs=set(),
        cfg=_cfg(tmp_path / "book", RecordingRuntime([])),
    )

    assert [issue for issue in issues if issue.kind == "quality"] == []
