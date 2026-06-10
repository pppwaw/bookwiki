"""Tests for the in-loop MDX compile validation (the "LSP in the loop").

``validate_mdx`` shells out to the bundled Node validator (``tools/mdx-validate``),
which compiles content with ``@mdx-js/mdx`` + remark-math - the same parser config as
the fumadocs site. ``check_node`` runs it on every rendered chapter and raises a
``MDX_PARSE_ERROR`` issue; the repair loop drives ``ChapterMdxRepairAgent`` to rewrite
the offending math into LaTeX.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bookwiki.agents.chapter_mdx_repair_agent import ChapterMdxRepairAgent
from bookwiki.agents.concept_mdx_repair_agent import ConceptMdxRepairAgent
from bookwiki.checkers.mdx_validator import mdx_validator_available, validate_mdx
from bookwiki.pipeline.nodes import check_node
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.concept import ConceptResult
from tests.fakes import RecordingRuntime

needs_node = pytest.mark.skipif(
    not mdx_validator_available(),
    reason="node and tools/mdx-validate dependencies are required",
)


# --------------------------------------------------------------------------- #
# validate_mdx - matches the site parser (catches the real build breakers,
# does not false-positive on math)
# --------------------------------------------------------------------------- #
@needs_node
def test_validate_mdx_flags_bare_comparison() -> None:
    errors = validate_mdx("当 n 较小（比如 n<30）时不准确。")
    assert errors
    assert any("3" in error or "name" in error.lower() for error in errors)


@needs_node
def test_validate_mdx_flags_bare_set_notation() -> None:
    errors = validate_mdx("拒绝域为 {z ≥ zα}。")
    assert errors
    assert any("acorn" in error.lower() or "expression" in error.lower() for error in errors)


@needs_node
def test_validate_mdx_accepts_latex_math() -> None:
    body = "样本均值 $\\bar{X} = \\frac{1}{n}\\sum X_i$ 服从 $N(\\mu, \\sigma^2)$。"
    assert validate_mdx(body) == []


@needs_node
def test_validate_mdx_accepts_book_figure_tag() -> None:
    body = '# 标题\n\n正文里有 $\\mu$。\n\n<BookFigure id="paper-p001-b001" />\n'
    assert validate_mdx(body) == []


@needs_node
def test_validate_mdx_flags_bare_jsx_expression() -> None:
    # An inline <cite> wrapping bare LaTeX `\bar{X}` compiles, but `{X}` renders as JS
    # and throws `ReferenceError: X is not defined` at prerender. The scan must catch it.
    errors = validate_mdx("统计量 <cite ref_id=\"p1\">Z = \\bar{X} / \\sqrt{n}</cite>。")
    assert errors
    assert any("bare JSX expression" in error for error in errors)


@needs_node
def test_validate_mdx_allows_braces_inside_math() -> None:
    # The same braces are safe inside $...$ - remark-math consumes them as LaTeX, so they
    # never become JSX expressions.
    assert validate_mdx("统计量 $Z = \\bar{X} / \\sqrt{n}$。") == []


def test_mdx_validator_available_returns_bool() -> None:
    assert isinstance(mdx_validator_available(), bool)


# --------------------------------------------------------------------------- #
# check_node - raises MDX_PARSE_ERROR for an uncompilable rendered chapter
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@needs_node
@pytest.mark.asyncio
async def test_check_node_flags_uncompilable_chapter_mdx(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    docs = book_dir / "content" / "docs"
    _write(docs / "index.mdx", "---\ntitle: Book\n---\n\n## 目录\n")
    # A chapter with a bare comparison `n<30` plus the sections check_node expects,
    # so the only error is the MDX one.
    _write(
        docs / "chapters" / "chapter-1.mdx",
        "---\ntitle: Chapter 1\n---\n\n# Chapter 1\n\n"
        "当 n<30 时不准确。\n\n<QuizBlock></QuizBlock>\n\n## Anki Cards\n\n## Sources\n",
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    result = await check_node({"agent_results": {}, "concept_pages": {}}, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    mdx_issues = [i for i in report["issues"] if i["code"] == "MDX_PARSE_ERROR"]
    assert mdx_issues
    assert mdx_issues[0]["owner_task_id"] == "chapter-1:chapter"
    assert mdx_issues[0]["severity"] == "error"
    assert "chapter-1:chapter" in result["repair_targets"]


# --------------------------------------------------------------------------- #
# ChapterMdxRepairAgent - rewrites the body given the diagnostics
# --------------------------------------------------------------------------- #
def _repair_input() -> dict[str, object]:
    return {
        "chapter_id": "chapter-1",
        "title": "Chapter 1",
        "body_md": "# Chapter 1\n\n当 n<30 时不准确。",
        "concepts": ["t-distribution"],
        "citations": [{"ref_id": "src-p001", "quote": "small sample"}],
        "owner_task_id": "chapter-1:chapter",
        "mdx_errors": ["line 6, column 5: Unexpected character `3` before name"],
        "language": "zh-CN",
        "book_notes": "",
        "allowed_source_refs": ["src-p001"],
    }


@pytest.mark.asyncio
async def test_chapter_mdx_repair_agent_returns_fixed_body() -> None:
    fixed = {
        "chapter_id": "chapter-1",
        "title": "Chapter 1",
        "body_md": "# Chapter 1\n\n当 $n < 30$ 时不准确。",
        "concepts": ["t-distribution"],
        "citations": [{"ref_id": "src-p001", "quote": "small sample"}],
        "owner_task_id": "chapter-1:chapter",
    }
    runtime = RecordingRuntime([fixed])

    result = await ChapterMdxRepairAgent().run(
        _repair_input(), model="deepseek-v4-pro", runtime=runtime
    )

    assert isinstance(result, ChapterResult)
    assert "$n < 30$" in result.body_md
    assert "n<30" not in result.body_md
    assert result.owner_task_id == "chapter-1:chapter"
    # The diagnostics and body reach the prompt so the model can locate the fix.
    prompt = runtime.calls[0]["user"]
    assert "Unexpected character" in prompt
    assert "n<30" in prompt
    # Document mode carries allowed refs in the input and validates them while parsing.
    assert '"allowed_source_refs"' in prompt


@pytest.mark.asyncio
async def test_chapter_mdx_repair_agent_echoes_draft_offline() -> None:
    # Under TestLLMRuntime the draft (unchanged body) is echoed - the deterministic
    # offline path keeps the agent runnable without a real model.
    result = await ChapterMdxRepairAgent().run(
        _repair_input(), model="stub", runtime=TestLLMRuntime()
    )
    assert result.chapter_id == "chapter-1"
    assert result.owner_task_id == "chapter-1:chapter"


# --------------------------------------------------------------------------- #
# Concept pages - check_node validates them too (not just chapters)
# --------------------------------------------------------------------------- #
@needs_node
@pytest.mark.asyncio
async def test_check_node_flags_uncompilable_concept_mdx(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    docs = book_dir / "content" / "docs"
    _write(docs / "index.mdx", "---\ntitle: Book\n---\n\n## 目录\n")
    # A concept page with a stray inline <cite> tag and bare math: both break MDX.
    _write(
        docs / "concepts" / "Sample-size.mdx",
        "---\ntitle: Sample size\n---\n\n# Sample size\n\n"
        '需要 <cite ref_id="p001">Solve {\\frac{a}{b}} \\le 10</cite> 个观测。\n',
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    result = await check_node({"agent_results": {}, "concept_pages": {}}, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    mdx_issues = [i for i in report["issues"] if i["code"] == "MDX_PARSE_ERROR"]
    assert mdx_issues
    assert mdx_issues[0]["owner_task_id"] == "concept-mdx:Sample-size"
    assert "concept-mdx:Sample-size" in result["repair_targets"]


# --------------------------------------------------------------------------- #
# ConceptMdxRepairAgent - fixes bare math + strips stray inline <cite> tags
# --------------------------------------------------------------------------- #
def _concept_repair_input() -> dict[str, object]:
    return {
        "name": "Sample size",
        "summary_md": "样本量确定。",
        "body_md": '需要 <cite ref_id="p001">Solve {\\frac{a}{b}} \\le 10</cite> 个观测。',
        "related": ["Confidence interval"],
        "citations": [{"ref_id": "p001", "quote": "solve"}],
        "owner_task_id": "concept:Sample size",
        "mdx_errors": ["line 7: Expected a closing tag for `<cite>`"],
        "language": "zh-CN",
        "book_notes": "",
        "allowed_source_refs": ["p001"],
    }


@pytest.mark.asyncio
async def test_concept_mdx_repair_agent_returns_fixed_body() -> None:
    fixed = {
        "name": "Sample size",
        "summary_md": "样本量确定。",
        "body_md": "需要 $\\frac{a}{b} \\le 10$ 个观测。",
        "related": ["Confidence interval"],
        "citations": [{"ref_id": "p001", "quote": "solve"}],
        "owner_task_id": "concept:Sample size",
    }
    runtime = RecordingRuntime([fixed])

    result = await ConceptMdxRepairAgent().run(
        _concept_repair_input(), model="deepseek-v4-pro", runtime=runtime
    )

    assert isinstance(result, ConceptResult)
    assert "<cite" not in result.body_md
    assert "$\\frac{a}{b} \\le 10$" in result.body_md
    assert result.owner_task_id == "concept:Sample size"
    # The diagnostics + body reach the prompt so the model can locate the fix.
    prompt = runtime.calls[0]["user"]
    assert "closing tag for `<cite>`" in prompt
    assert "<cite" in prompt
    assert '"allowed_source_refs"' in prompt


@pytest.mark.asyncio
async def test_concept_mdx_repair_agent_echoes_draft_offline() -> None:
    result = await ConceptMdxRepairAgent().run(
        _concept_repair_input(), model="stub", runtime=TestLLMRuntime()
    )
    assert result.name == "Sample size"
    assert result.owner_task_id == "concept:Sample size"
