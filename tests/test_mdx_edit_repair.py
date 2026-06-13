"""Unit tests for the surgical MDX edit-repair loop (editor semantics + budgets)."""

from __future__ import annotations

import pytest

from bookwiki.agents.mdx_edit_repair import (
    ChapterMdxEditRepairAgent,
    MdxBodyEditor,
    repair_body_with_edit_tools,
)
from tests.fakes import RecordingRuntime

# --------------------------------------------------------------------------- #
# MdxBodyEditor - Anthropic str_replace semantics
# --------------------------------------------------------------------------- #


def test_editor_view_returns_numbered_clamped_range() -> None:
    editor = MdxBodyEditor("alpha\nbeta\ngamma")
    out = editor.view(2, 99)
    assert out["ok"] is True
    assert out["total_lines"] == 3
    assert out["content"] == "2: beta\n3: gamma"


def test_editor_view_rejects_inverted_range() -> None:
    assert MdxBodyEditor("a\nb").view(2, 1)["ok"] is False


def test_editor_str_replace_applies_unique_match() -> None:
    editor = MdxBodyEditor("当 n<30 时不准确。")
    assert editor.str_replace("n<30", "$n < 30$") == {"ok": True}
    assert editor.text == "当 $n < 30$ 时不准确。"


def test_editor_str_replace_rejects_zero_matches_with_anthropic_error() -> None:
    editor = MdxBodyEditor("body text")
    out = editor.str_replace("missing", "x")
    assert out["ok"] is False
    assert "No match found" in out["error"]
    assert editor.text == "body text"


def test_editor_str_replace_rejects_multiple_matches() -> None:
    editor = MdxBodyEditor("dup\ndup\n")
    out = editor.str_replace("dup", "x")
    assert out["ok"] is False
    assert "Found 2 matches" in out["error"]
    assert editor.text == "dup\ndup\n"


def test_editor_str_replace_rejects_empty_old_str() -> None:
    assert MdxBodyEditor("a").str_replace("", "x")["ok"] is False


# --------------------------------------------------------------------------- #
# repair_body_with_edit_tools - best-snapshot tracking and budgets
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_loop_keeps_fewest_error_snapshot_when_later_edit_regresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scripted validator: first edit -> 1 error left, second edit -> 3 errors (worse).
    outcomes = [["line 1: still broken"], ["e1", "e2", "e3"]]
    monkeypatch.setattr(
        "bookwiki.agents.mdx_edit_repair.validate_mdx",
        lambda _body: outcomes.pop(0),
    )
    runtime = RecordingRuntime(
        [{"status": "partial", "notes": ""}],
        tool_calls=[
            ("str_replace", {"old_str": "bad-one", "new_str": "good-one"}),
            ("str_replace", {"old_str": "bad-two", "new_str": "worse-two"}),
        ],
    )

    body, remaining = await repair_body_with_edit_tools(
        body_md="bad-one and bad-two",
        mdx_errors=["line 1: a", "line 1: b"],
        model="stub",
        runtime=runtime,
        agent_name="test",
        doc_label="chapter test",
    )

    # Best snapshot = after the FIRST edit (1 error), not the regressed second state.
    assert body == "good-one and bad-two"
    assert remaining == ["line 1: still broken"]


@pytest.mark.asyncio
async def test_loop_reports_done_and_uses_clean_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bookwiki.agents.mdx_edit_repair.validate_mdx", lambda _body: [])
    runtime = RecordingRuntime(
        [{"status": "fixed", "notes": ""}],
        tool_calls=[("str_replace", {"old_str": "n<30", "new_str": "$n < 30$"})],
    )

    body, remaining = await repair_body_with_edit_tools(
        body_md="当 n<30 时",
        mdx_errors=["line 1: Unexpected character"],
        model="stub",
        runtime=runtime,
        agent_name="test",
        doc_label="chapter test",
    )

    assert body == "当 $n < 30$ 时"
    assert remaining == []
    # The tool result told the model it is done.
    assert any("done" in result for result in runtime.tool_results if isinstance(result, dict))


@pytest.mark.asyncio
async def test_loop_failed_match_does_not_mutate_or_consume_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def _fake_validate(_body: str) -> list[str]:
        calls["n"] += 1
        return []

    monkeypatch.setattr("bookwiki.agents.mdx_edit_repair.validate_mdx", _fake_validate)
    runtime = RecordingRuntime(
        [{"status": "gave_up", "notes": ""}],
        tool_calls=[("str_replace", {"old_str": "absent", "new_str": "x"})],
    )

    body, remaining = await repair_body_with_edit_tools(
        body_md="original",
        mdx_errors=["line 1: e"],
        model="stub",
        runtime=runtime,
        agent_name="test",
        doc_label="chapter test",
    )

    assert body == "original"
    assert remaining == ["line 1: e"]
    assert calls["n"] == 0  # validator only runs after a SUCCESSFUL mutation
    assert "No match found" in runtime.tool_results[0]["error"]


@pytest.mark.asyncio
async def test_agent_passes_metadata_through_and_builds_error_windows() -> None:
    runtime = RecordingRuntime(
        [{"status": "fixed", "notes": ""}],
        tool_calls=[("str_replace", {"old_str": "{bad}", "new_str": "$bad$"})],
    )
    inp = {
        "chapter_id": "chapter-9-2",
        "title": "9.2 Infinite Series",
        "body_md": "line one\n{bad} expression here\nline three",
        "concepts": ["series"],
        "citations": [{"ref_id": "p001", "quote": "q"}],
        "owner_task_id": "chapter-9-2:chapter",
        "mdx_errors": ["line 2, column 1: Could not parse expression with acorn"],
        "language": "zh-CN",
    }

    result = await ChapterMdxEditRepairAgent().run(inp, model="stub", runtime=runtime)

    assert result.chapter_id == "chapter-9-2"
    assert result.title == "9.2 Infinite Series"
    assert result.concepts == ["series"]
    assert result.citations[0].ref_id == "p001"
    assert "$bad$" in result.body_md
    # The prompt embeds a numbered window around the reported error line.
    prompt = runtime.calls[0]["user"]
    assert "2: {bad} expression here" in prompt
