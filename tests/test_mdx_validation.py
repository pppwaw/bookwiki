"""Tests for the in-loop MDX compile validation (the "LSP in the loop").

``validate_mdx`` shells out to the bundled Node validator (``tools/mdx-validate``),
which compiles content with ``@mdx-js/mdx`` + remark-math - the same parser config as
the fumadocs site. ``check_node`` runs it on every rendered chapter and raises a
``MDX_PARSE_ERROR`` issue; the repair loop drives ``ChapterMdxEditRepairAgent`` to fix
the offending math via surgical ``view``/``str_replace`` edit tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bookwiki.agents.mdx_edit_repair import (
    ChapterMdxEditRepairAgent,
    ConceptMdxEditRepairAgent,
    MdxEditRepairAgent,
    MdxRepairResult,
)
from bookwiki.checkers.mdx_validator import (
    mdx_validator_available,
    validate_mdx,
    validate_mdx_many,
)
from bookwiki.generate.validate_artifact import validate_artifact
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
@pytest.mark.asyncio
async def test_validate_artifact_pre_normalizes_deterministic_mdx_fixes(tmp_path: Path) -> None:
    cfg = BookConfig(book_dir=tmp_path / "book", book_id="book", title="Book")
    body = (
        "| 数学联系 | $F(\\omega) = F(s)\\bigl|_{s=j\\omega}$ | |\n"
        '<cite ref="p001"/>\n'
        "![figure](bookwiki-assets/source/figure.jpg)\n"
    )

    issues = await validate_artifact(body_md=body, kind="concept", allowed_refs=set(), cfg=cfg)

    assert [issue for issue in issues if issue.kind == "mdx"] == []


@needs_node
def test_validate_mdx_accepts_book_figure_tag() -> None:
    body = '# 标题\n\n正文里有 $\\mu$。\n\n<BookFigure id="paper-p001-b001" />\n'
    assert validate_mdx(body) == []


@needs_node
def test_validate_mdx_flags_bare_jsx_expression() -> None:
    # An inline <cite> wrapping bare LaTeX `\bar{X}` compiles, but `{X}` renders as JS
    # and throws `ReferenceError: X is not defined` at prerender. The scan must catch it.
    errors = validate_mdx('统计量 <cite ref_id="p1">Z = \\bar{X} / \\sqrt{n}</cite>。')
    assert errors
    assert any("bare JSX expression" in error for error in errors)


@needs_node
def test_validate_mdx_allows_braces_inside_math() -> None:
    # The same braces are safe inside $...$ - remark-math consumes them as LaTeX, so they
    # never become JSX expressions.
    assert validate_mdx("统计量 $Z = \\bar{X} / \\sqrt{n}$。") == []


# --------------------------------------------------------------------------- #
# allowlist scan - reject render-time-unsafe JSX that still *compiles*
# (the `<cite ref=...>` RSC crash class), without false-flagging safe content
# --------------------------------------------------------------------------- #
@needs_node
def test_validate_mdx_flags_cite_ref_prop() -> None:
    # `<cite ref=...>` compiles as MDX, but `ref` is a reserved React prop that crashes
    # Server Component prerender ("Refs cannot be used in Server Components"). The compile
    # and bare-expression layers both pass it, so the allowlist scan must reject it.
    errors = validate_mdx('定理 <cite ref="12.4-p011">表述</cite>。')
    assert errors
    assert any('disallowed prop "ref"' in error for error in errors)


@needs_node
def test_validate_mdx_flags_jsx_event_handler_prop() -> None:
    errors = validate_mdx('<span onClick="doThing()">点我</span>')
    assert errors
    assert any('disallowed prop "onClick"' in error for error in errors)


@needs_node
def test_validate_mdx_flags_raw_script_element() -> None:
    errors = validate_mdx("正文。\n\n<script>doThing()</script>\n")
    assert errors
    assert any("<script>" in error for error in errors)


@needs_node
def test_validate_mdx_allows_safe_components_and_html() -> None:
    # SourceRef citations and plain inline HTML must not be false-flagged by the scan.
    body = (
        '结论成立 <SourceRef id={"12.4-p011"} quote={"theorem"} />。\n\n'
        "下标 <sub>i</sub> 与 <strong>强调</strong> 都是安全的。\n"
    )
    assert validate_mdx(body) == []


def test_mdx_validator_available_returns_bool() -> None:
    assert isinstance(mdx_validator_available(), bool)


@needs_node
def test_validate_mdx_many_reports_per_file_and_localises_the_bad_one() -> None:
    files = {
        "good-1": "样本均值 $\\bar{X}$ 是干净的。",
        "bad": "当 n<30 时不准确。",
        "good-2": "# 标题\n\n另一段干净正文。",
    }
    results = validate_mdx_many(files, max_files=2)  # force >1 batch
    assert set(results) == {"good-1", "bad", "good-2"}
    assert results["good-1"] == []
    assert results["good-2"] == []
    assert results["bad"], "the bad file's MDX error must be reported"


@needs_node
def test_validate_mdx_many_matches_single_file_results() -> None:
    samples = {
        "a": "拒绝域为 {z ≥ zα}。",  # bare expression
        "b": "干净的 $x^2$ 数学。",
    }
    batched = validate_mdx_many(samples)
    for key, content in samples.items():
        assert batched[key] == validate_mdx(content)


def test_validate_mdx_many_returns_empty_when_validator_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("subprocess.run should not be called when validator is unavailable")

    monkeypatch.setattr("bookwiki.checkers.mdx_validator.mdx_validator_available", lambda: False)
    monkeypatch.setattr("bookwiki.checkers.mdx_validator.subprocess.run", fake_run)

    assert validate_mdx_many({"a": "some mdx", "b": "more mdx"}) == {"a": [], "b": []}
    assert called is False


def test_validate_mdx_many_empty_input_returns_empty() -> None:
    assert validate_mdx_many({}) == {}


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


@needs_node
@pytest.mark.asyncio
async def test_check_node_exempts_exam_page_from_pedagogical_section_checks(tmp_path: Path) -> None:
    # ``exam.mdx`` is a structural page: the teaching body lives in the sibling ``index.mdx``,
    # the exam page legitimately carries no QuizBlock/Anki/Sources. check_node must NOT flag it.
    book_dir = tmp_path / "book"
    docs = book_dir / "content" / "docs"
    _write(docs / "index.mdx", "---\ntitle: Book\n---\n\n## 目录\n")
    _write(
        docs / "chapters" / "chapter-1" / "exam.mdx",
        "---\ntitle: Chapter 1 · 测验\n---\n\n# 测验\n\n干净的题面，没有教学区块。\n",
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    result = await check_node({"agent_results": {}, "concept_pages": {}}, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    pedagogical = {"MISSING_QUIZ", "MISSING_ANKI", "MISSING_SOURCES"}
    assert [i for i in report["issues"] if i["code"] in pedagogical] == []
    assert result["repair_targets"] == []


@needs_node
@pytest.mark.asyncio
async def test_check_node_reports_missing_quiz_as_warning_not_repair_target(tmp_path: Path) -> None:
    # A teaching chapter may legitimately ship without a quiz (the section agent is allowed to
    # skip questions when content is thin). MISSING_QUIZ has no deterministic repair, so it is a
    # ``warning`` that gets recorded but never enters repair_targets (no futile repair rounds).
    book_dir = tmp_path / "book"
    docs = book_dir / "content" / "docs"
    _write(docs / "index.mdx", "---\ntitle: Book\n---\n\n## 目录\n")
    _write(
        docs / "chapters" / "chapter-1.mdx",
        "---\ntitle: Chapter 1\n---\n\n# Chapter 1\n\n干净的正文，没有任何教学区块。\n",
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    result = await check_node({"agent_results": {}, "concept_pages": {}}, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    missing_quiz = [i for i in report["issues"] if i["code"] == "MISSING_QUIZ"]
    assert missing_quiz
    assert missing_quiz[0]["severity"] == "warning"
    assert missing_quiz[0]["owner_task_id"] == "chapter-1:quiz"
    # warnings never trigger repair — the target must stay out of repair_targets.
    assert "chapter-1:quiz" not in result["repair_targets"]


# --------------------------------------------------------------------------- #
# ChapterMdxEditRepairAgent - surgical str_replace edits driven by diagnostics
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
async def test_chapter_mdx_edit_repair_agent_fixes_body_via_str_replace() -> None:
    runtime = RecordingRuntime(
        [{"status": "fixed", "notes": "wrapped the comparison in math"}],
        tool_calls=[
            ("view", {"start_line": 1, "end_line": 3}),
            ("str_replace", {"old_str": "n<30", "new_str": "$n < 30$"}),
        ],
    )

    result = await ChapterMdxEditRepairAgent().run(
        _repair_input(), model="deepseek-v4-pro", runtime=runtime
    )

    assert isinstance(result, ChapterResult)
    # The repaired body comes from the editor state, not from the model's answer.
    assert "$n < 30$" in result.body_md
    assert "n<30" not in result.body_md
    # Metadata is passed through unchanged (never re-generated by the model).
    assert result.owner_task_id == "chapter-1:chapter"
    assert result.concepts == ["t-distribution"]
    assert result.citations[0].ref_id == "src-p001"
    # The diagnostics and an error-line window reach the prompt.
    prompt = runtime.calls[0]["user"]
    assert "Unexpected character" in prompt
    assert "n<30" in prompt
    # Both tool invocations succeeded against the in-memory editor.
    assert runtime.tool_results[0]["ok"] is True
    assert "1: # Chapter 1" in runtime.tool_results[0]["content"]
    assert runtime.tool_results[1]["ok"] is True


@pytest.mark.asyncio
async def test_chapter_mdx_edit_repair_agent_offline_keeps_body() -> None:
    # Under TestLLMRuntime no tools run and the draft outcome is echoed - the body
    # passes through unchanged and the agent stays runnable offline.
    result = await ChapterMdxEditRepairAgent().run(
        _repair_input(), model="stub", runtime=TestLLMRuntime()
    )
    assert result.chapter_id == "chapter-1"
    assert result.owner_task_id == "chapter-1:chapter"
    assert result.body_md == "# Chapter 1\n\n当 n<30 时不准确。"


@pytest.mark.asyncio
async def test_mdx_edit_repair_agent_edits_full_mdx_in_place() -> None:
    # Post-integrate repair edits the rendered .mdx DIRECTLY (not body_md): the str_replace
    # runs against the full file, so frontmatter/heading survive and error lines align.
    runtime = RecordingRuntime(
        [{"status": "fixed", "notes": "wrapped the comparison in math"}],
        tool_calls=[("str_replace", {"old_str": "n<30", "new_str": "$n < 30$"})],
    )
    mdx = "---\ntitle: Chapter 1\n---\n\n# Chapter 1\n\n当 n<30 时不准确。\n"

    result = await MdxEditRepairAgent().run(
        {
            "mdx": mdx,
            "mdx_errors": ["line 7, column 5: Unexpected character `3`"],
            "language": "zh-CN",
            "doc_label": "chapter-1:chapter",
        },
        model="deepseek-v4-pro",
        runtime=runtime,
    )

    assert isinstance(result, MdxRepairResult)
    assert "$n < 30$" in result.mdx
    assert "n<30" not in result.mdx
    # Frontmatter + heading (only present in the rendered .mdx, never in body_md) survive.
    assert result.mdx.startswith("---\ntitle: Chapter 1\n---")
    assert "# Chapter 1" in result.mdx


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
# ConceptMdxEditRepairAgent - fixes bare math / stray inline tags surgically
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
async def test_concept_mdx_edit_repair_agent_fixes_body_via_str_replace() -> None:
    runtime = RecordingRuntime(
        [{"status": "fixed", "notes": "replaced cite tag with inline math"}],
        tool_calls=[
            (
                "str_replace",
                {
                    "old_str": '<cite ref_id="p001">Solve {\\frac{a}{b}} \\le 10</cite>',
                    "new_str": "$\\frac{a}{b} \\le 10$",
                },
            ),
        ],
    )

    result = await ConceptMdxEditRepairAgent().run(
        _concept_repair_input(), model="deepseek-v4-pro", runtime=runtime
    )

    assert isinstance(result, ConceptResult)
    assert "<cite" not in result.body_md
    assert "$\\frac{a}{b} \\le 10$" in result.body_md
    assert result.owner_task_id == "concept:Sample size"
    assert result.related == ["Confidence interval"]
    assert result.citations[0].ref_id == "p001"
    prompt = runtime.calls[0]["user"]
    assert "closing tag for `<cite>`" in prompt
    assert "<cite" in prompt


@pytest.mark.asyncio
async def test_concept_mdx_edit_repair_agent_offline_keeps_body() -> None:
    result = await ConceptMdxEditRepairAgent().run(
        _concept_repair_input(), model="stub", runtime=TestLLMRuntime()
    )
    assert result.name == "Sample size"
    assert result.owner_task_id == "concept:Sample size"
    assert "<cite" in result.body_md
