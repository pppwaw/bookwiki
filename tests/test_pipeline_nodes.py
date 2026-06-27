from __future__ import annotations

from types import SimpleNamespace

from bookwiki.integrator.markdown_renderers import normalize_public_asset_markdown_images
from bookwiki.pipeline.nodes import (
    _drop_missing_local_markdown_links,
    _illegal_component_fence_issues,
    _strip_illegal_component_fences,
    _target_mdx_path,
)


def test_drop_missing_local_markdown_links_keeps_prose_and_existing_links(tmp_path) -> None:
    existing = tmp_path / "target.mdx"
    existing.write_text("# target\n", encoding="utf-8")
    body = (
        "See [valid](target), [missing](/Chapter-12-Three-phase-Circuits/section-003), "
        "[web](https://example.com), and `code [missing](/nope)`."
    )

    out = _drop_missing_local_markdown_links(body, tmp_path)

    assert "[valid](target)" in out
    assert "missing" in out
    assert "](/Chapter-12-Three-phase-Circuits/section-003)" not in out
    assert "[web](https://example.com)" in out
    assert "`code [missing](/nope)`" in out


def test_normalize_public_asset_markdown_images_makes_book_assets_root_relative() -> None:
    body = (
        "![figure](bookwiki-assets/source/figure.jpg) "
        "![local](images/local.jpg) "
        "![web](https://example.com/figure.jpg) "
        "`![code](bookwiki-assets/source/raw.jpg)`"
    )

    out = normalize_public_asset_markdown_images(body)

    assert "![figure](/bookwiki-assets/source/figure.jpg)" in out
    assert "![local](images/local.jpg)" in out
    assert "![web](https://example.com/figure.jpg)" in out
    assert "`![code](bookwiki-assets/source/raw.jpg)`" in out


# --- Task B: 非法代码围栏包裹 MDX 组件（``` ```quiz ``` 触发 ShikiError 的根因） ---

_QUIZ_FENCE = (
    "讲解结束。\n\n"
    "```quiz\n"
    "<QuizBlock>\n"
    '<QuizItem answer="choice-1">\n'
    "<QuizQuestion>问？</QuizQuestion>\n"
    "</QuizItem>\n"
    "</QuizBlock>\n"
    "```\n\n"
    "后续正文。\n"
)


def test_illegal_component_fence_flags_quiz_fence() -> None:
    owner = "Chapter-19-Two-Port-Networks/index:chapter"
    issues = _illegal_component_fence_issues(_QUIZ_FENCE, owner)

    assert len(issues) == 1
    assert issues[0].code == "ILLEGAL_CODE_FENCE"
    assert issues[0].severity == "error"
    assert issues[0].owner_task_id == owner


def test_illegal_component_fence_exempts_mermaid_and_plain_code() -> None:
    text = (
        "```mermaid\nflowchart TD\n  A --> B\n```\n\n"
        '```python\nprint("<QuizBlock> 仅是字符串示例")\n```\n'
    )

    # mermaid 是合法结构图围栏；python 块里的 <QuizBlock 不在行首，是字符串示例，不应误报。
    assert _illegal_component_fence_issues(text, "x:chapter") == []


def test_strip_illegal_component_fences_unwraps_but_keeps_block() -> None:
    out = _strip_illegal_component_fences(_QUIZ_FENCE)

    assert "```quiz" not in out
    assert "<QuizBlock>" in out  # 组件本体保留，仅去掉围栏
    assert "</QuizBlock>" in out
    assert "讲解结束。" in out and "后续正文。" in out
    # 剥离后不应再有非法围栏
    assert _illegal_component_fence_issues(out, "x:chapter") == []


# --- Task A: owner_task_id 携带章节相对路径，消除同名文件（index.mdx/exam.mdx）碰撞 ---


def test_target_mdx_path_resolves_per_chapter_without_collision(tmp_path) -> None:
    chapters = tmp_path / "chapters"
    (chapters / "Chapter-1-Basics").mkdir(parents=True)
    (chapters / "Chapter-19-Two-Port-Networks").mkdir(parents=True)
    file_a = chapters / "Chapter-1-Basics" / "index.mdx"
    file_b = chapters / "Chapter-19-Two-Port-Networks" / "index.mdx"
    file_a.write_text("---\ntitle: a\n---\n", encoding="utf-8")
    file_b.write_text("---\ntitle: b\n---\n", encoding="utf-8")
    cfg = SimpleNamespace(content_dir=tmp_path)

    # 两个不同章节的同名 index.mdx 必须各自反查回自己，而非都命中第一个。
    assert _target_mdx_path("Chapter-1-Basics/index:chapter", cfg) == file_a
    assert _target_mdx_path("Chapter-19-Two-Port-Networks/index:chapter", cfg) == file_b


def test_target_mdx_path_returns_none_for_missing_file(tmp_path) -> None:
    (tmp_path / "chapters").mkdir()
    cfg = SimpleNamespace(content_dir=tmp_path)

    assert _target_mdx_path("Chapter-404/index:chapter", cfg) is None
