from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

from bookwiki.pipeline.nodes import _source_citation_md, integrate_node
from bookwiki.scheduler.config import BookConfig


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def agent_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "_schema_version": "llm.v1",
        "_agent": "FixtureAgent",
        "_model": "stub",
        "result": result,
    }


def test_source_citations_do_not_repair_bare_latex_expressions() -> None:
    markdown = _source_citation_md(
        [
            {
                "ref_id": "Week-10-p023",
                "quote": r"\frac { 1 } { n } is not unbiased",
            },
            {
                "ref_id": "source-p004",
                "quote": r"Display math may be written as \[E(X)=\theta\].",
            },
        ]
    )

    assert r"\frac &#123; 1 &#125; &#123; n &#125; is not unbiased" in markdown
    assert "\n\n$$\nE(X)=\\theta\n$$\n\n" in markdown
    assert r"$\frac$" not in markdown


def test_source_citations_wrap_pure_bare_latex_in_inline_math() -> None:
    markdown = _source_citation_md(
        [
            {
                "ref_id": "9.2-p008",
                "quote": r"\frac{1}{(k+2)(k+3)} = \frac{1}{(k+2)} - \frac{1}{(k+3)}",
            },
            {
                "ref_id": "9.2-p010",
                "quote": r"S_{n} = \ln \frac{2}{1} + \dots = \ln(n+1) \rightarrow \infty",
            },
        ]
    )

    assert r"`9.2-p008`: $\frac{1}{(k+2)(k+3)} = \frac{1}{(k+2)} - \frac{1}{(k+3)}$" in markdown
    assert (
        r"`9.2-p010`: $S_{n} = \ln \frac{2}{1} + \dots = \ln(n+1) \rightarrow \infty$" in markdown
    )
    # raw braces must survive inside the wrapped math (no HTML-escaping) so KaTeX parses.
    assert "&#123;" not in markdown


def test_source_citations_wrap_only_math_suffix_after_prose_label() -> None:
    markdown = _source_citation_md(
        [
            {
                "ref_id": "9.2-p005",
                "quote": r"The N-th partial sum: S_n = a_1 + \dots + a_n = \sum_{k=1}^{n} a_k",
            },
        ]
    )

    assert (
        r"`9.2-p005`: The N-th partial sum: $S_n = a_1 + \dots + a_n = \sum_{k=1}^{n} a_k$"
        in markdown
    )


def test_source_citations_leave_already_delimited_and_plain_prose_untouched() -> None:
    markdown = _source_citation_md(
        [
            {
                "ref_id": "9.2-p013",
                "quote": r"A geometric series $\sum_{k=1}^{\infty} a r^{k-1}$ with $a \neq 0$.",
            },
            {
                "ref_id": "9.2-p006",
                "quote": "The infinite series converges and has sum S.",
            },
        ]
    )

    # Already-delimited math is idempotent: not re-wrapped, dollars preserved.
    assert (
        r"`9.2-p013`: A geometric series $\sum_{k=1}^{\infty} a r^{k-1}$ with $a \neq 0$."
        in markdown
    )
    # Plain prose with no LaTeX signal is never wrapped.
    assert "`9.2-p006`: The infinite series converges and has sum S." in markdown


def test_integrate_node_renders_fixed_agent_results_to_mdx_snapshot(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    concept_dir = result_dir / "concepts"
    write_json(
        result_dir / "chapter-1.chapter.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nCore idea [[states]].\n\nSecond paragraph.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "source-p001", "quote": "State space search"}],
                "owner_task_id": "chapter-1:chapter",
            }
        ),
    )
    write_json(
        result_dir / "chapter-1.summary.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "summary_md": "Search summary.",
                "key_points": ["State space"],
                "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                "owner_task_id": "chapter-1:summary",
            }
        ),
    )
    write_json(
        result_dir / "chapter-1.quiz.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "items": [],
                "owner_task_id": "chapter-1:quiz",
            }
        ),
    )
    write_json(
        result_dir / "chapter-1.card.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "items": [
                    {
                        "front": "State space",
                        "back": "Reachable states for search.",
                        "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                    }
                ],
                "owner_task_id": "chapter-1:card",
            }
        ),
    )
    write_json(
        concept_dir / "state-space.json",
        {
            "name": "state space",
            "summary_md": "State space is the reachable-state set.",
            "body_md": (
                "State space contains reachable states.\n\n"
                "This second paragraph should stay out of hover previews."
            ),
            "related": [],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "concept:state space",
        },
    )
    write_json(
        book_dir / "work" / "concepts" / "reconciled.json",
        {
            "concepts": [
                {
                    "canonical": "state space",
                    "aliases": ["states"],
                    "source_chapter_ids": ["chapter-1"],
                }
            ],
            "alias_map": {"states": "state space", "statespace": "state space"},
        },
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")
    state = {
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        },
        "reconciled_concepts": "work/concepts/reconciled.json",
        "concept_pages": {"state space": "work/agent_results/concepts/state-space.json"},
    }

    result = integrate_node(state, cfg)

    assert result == {"content_ready": True, "content_index": "site/content/docs/index.mdx"}
    docs = book_dir / "site" / "content" / "docs"
    chapter_mdx = (docs / "chapters" / "chapter-1.mdx").read_text(encoding="utf-8")
    concept_mdx = (docs / "concepts" / "state-space.mdx").read_text(encoding="utf-8")
    index_mdx = (docs / "index.mdx").read_text(encoding="utf-8")

    chapter_preview = (
        '<PreviewLink href={"/docs/concepts/state-space"} title={"state space"} '
        'summary={"State space is the reachable-state set."}>state space</PreviewLink>'
    )
    backlink_preview = (
        '<PreviewLink href={"/docs/chapters/chapter-1"} title={"Search"} '
        'summary={"Search summary."}>Search</PreviewLink>'
    )

    expected_chapter = dedent(
        """\
        ---
        chapter_id: chapter-1
        title: Search
        type: chapter
        order_index: 0
        summary: Search summary.
        concepts:
        - state space
        key_points:
        - State space
        ---

        # Search

        Core idea CHAPTER_PREVIEW.

        Second paragraph.

        ## Sources

        - `source-p001`: State space search

        ## Anki Cards

        <AnkiDeck cardIds={[
          "card-001"
        ]}>
        <AnkiCard id={"card-001"} citations={[
          {
            "ref_id": "source-p001",
            "quote": "State space"
          }
        ]}>
        <AnkiFront>
        State space
        </AnkiFront>
        <AnkiBack>
        Reachable states for search.
        </AnkiBack>
        </AnkiCard>
        </AnkiDeck>
        """
    ).replace("CHAPTER_PREVIEW", chapter_preview)
    assert chapter_mdx == expected_chapter
    expected_concept = dedent(
        """\
        ---
        title: state space
        type: concept
        summary: State space is the reachable-state set.
        ---

        # state space

        State space contains reachable states.

        This second paragraph should stay out of hover previews.

        ## Referenced By

        - BACKLINK_PREVIEW
        """
    ).replace("BACKLINK_PREVIEW", backlink_preview)
    assert concept_mdx == expected_concept
    chapter_card = (
        '<Card title={"Search"} href={"/docs/chapters/chapter-1"} '
        'description={"Search summary."} />'
    )
    concept_card = (
        '<Card title={"state space"} href={"/docs/concepts/state-space"} '
        'description={"State space is the reachable-state set."} />'
    )
    assert index_mdx == (
        dedent(
            """\
            ---
            title: Book
            description: Book 的互动学习指南：章节目录与核心概念。
            ---

            ## 目录

            <Cards>
              CHAPTER_CARD
            </Cards>

            ## 概念

            <Cards>
              CONCEPT_CARD
            </Cards>
            """
        )
        .replace("CHAPTER_CARD", chapter_card)
        .replace("CONCEPT_CARD", concept_card)
    )


def test_integrate_node_resolves_chapter_figures_from_source(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    source_md = (
        "# Chapter 1 source\n\n"
        '<BookFigure id="paper-p001-b001" src="/bookwiki-assets/paper-p001-b001.png" '
        'caption="Search tree diagram" />\n\n'
        "Body prose.\n\n"
        '<BookFigure id="paper-p001-b002" src="/bookwiki-assets/paper-p001-b002.png" '
        'caption="Heuristic comparison" />\n'
    )
    source_path = book_dir / "work" / "chapter-sources" / "chapter-1.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_md, encoding="utf-8")

    body_md = (
        "# Search\n\n"
        "Intro paragraph.\n\n"
        '<BookFigure id="paper-p001-b001" />\n\n'
        "Bridge text.\n\n"
        '<BookFigure id="paper-p999-b001" />\n\n'
        "Closing paragraph."
    )
    write_json(
        result_dir / "chapter-1.chapter.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": body_md,
                "concepts": [],
                "citations": [{"ref_id": "source-p001", "quote": "State space search"}],
                "owner_task_id": "chapter-1:chapter",
            }
        ),
    )
    write_json(
        result_dir / "chapter-1.summary.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "summary_md": "Search summary.",
                "key_points": [],
                "citations": [],
                "owner_task_id": "chapter-1:summary",
            }
        ),
    )
    write_json(
        result_dir / "chapter-1.quiz.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "items": [],
                "owner_task_id": "chapter-1:quiz",
            }
        ),
    )
    write_json(
        result_dir / "chapter-1.card.json",
        agent_payload(
            {
                "chapter_id": "chapter-1",
                "items": [],
                "owner_task_id": "chapter-1:card",
            }
        ),
    )
    write_json(
        book_dir / "work" / "concepts" / "reconciled.json",
        {"concepts": [], "alias_map": {}},
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")
    state = {
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        },
        "reconciled_concepts": "work/concepts/reconciled.json",
        "concept_pages": {},
        "chapter_sources": {"chapter-1": "work/chapter-sources/chapter-1.md"},
    }

    result = integrate_node(state, cfg)

    assert result == {"content_ready": True, "content_index": "site/content/docs/index.mdx"}
    chapter_mdx = (book_dir / "site" / "content" / "docs" / "chapters" / "chapter-1.mdx").read_text(
        encoding="utf-8"
    )

    canonical_b001 = (
        '<BookFigure id="paper-p001-b001" src="/bookwiki-assets/paper-p001-b001.png" '
        'caption="Search tree diagram" />'
    )
    # Inline reference is rewritten to the source-backed canonical tag.
    assert canonical_b001 in chapter_mdx
    assert '<BookFigure id="paper-p001-b001" />' not in chapter_mdx
    # Hallucinated reference (absent from source) is dropped entirely.
    assert "paper-p999-b001" not in chapter_mdx
    # Unreferenced source figures are not appended to the rendered chapter.
    assert "## Figures" not in chapter_mdx
    assert "paper-p001-b002" not in chapter_mdx
    assert "## Anki Cards" in chapter_mdx


def test_normalize_concept_links_preserves_mermaid_fences() -> None:
    """Concept-link normalization must never inject ``<PreviewLink>`` into a fenced
    code block (e.g. ```mermaid), even when a concept first appears inside the fence
    or a node uses the ``[[label]]`` subroutine shape."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"欧姆定律": "Ohm's Law", "电阻": "Resistance"}
    previews = {
        "Ohm's Law": {"href": "/c/Ohm", "title": "Ohm's Law", "summary": "v=iR"},
        "Resistance": {"href": "/c/Res", "title": "Resistance", "summary": "R"},
    }
    body = (
        "开场白，先不提概念。\n\n"
        "```mermaid\n"
        "graph TD\n"
        "  A[电压源] --> B[电阻 R]\n"
        "  B --> C{欧姆定律}\n"
        "  D[[电阻]] --> B\n"
        "```\n\n"
        "围栏后再讲电阻与欧姆定律。"
    )

    out = _normalize_concept_links(body, alias_map, previews)
    fence = out.split("```mermaid", 1)[1].split("```", 1)[0]
    after = out.split("```", 2)[2]

    # Fence content is byte-for-byte preserved: no PreviewLink injected, node shapes intact.
    assert "<PreviewLink" not in fence
    assert "A[电压源] --> B[电阻 R]" in fence
    assert "C{欧姆定律}" in fence
    assert "D[[电阻]] --> B" in fence
    # Prose outside the fence is still linked as usual.
    assert "<PreviewLink" in after


def test_normalize_concept_links_preserves_multiline_display_math() -> None:
    """Concept-link normalization must never inject ``<PreviewLink>`` inside a MULTI-LINE
    ``$$ ... $$`` display block — the 14.4 ``$\\operatorname{<PreviewLink ...$`` break, where
    the interior line of a display block was treated as prose by per-line processing."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"散度": "Divergence"}
    previews = {"Divergence": {"href": "/c/Div", "title": "散度", "summary": "div"}}
    body = (
        "先讲背景。\n\n"
        "$$\n"
        "\\operatorname{散度} \\vec F = \\nabla \\cdot \\vec F\n"
        "$$\n\n"
        "再讲散度的意义。"
    )

    out = _normalize_concept_links(body, alias_map, previews)
    math = out.split("$$", 2)[1]  # content between the first pair of $$

    # Display-math interior is byte-for-byte preserved: no PreviewLink injected.
    assert "<PreviewLink" not in math
    assert "\\operatorname{散度}" in math
    # Prose outside the math is still linked as usual.
    assert "<PreviewLink" in out.split("$$", 2)[2]


def test_normalize_concept_links_resolves_chapter_wikilinks() -> None:
    """A ``[[chapter title]]`` resolves to a chapter PreviewLink; a name shared by a concept and a
    chapter resolves to the chapter (``[[ ]]`` is the explicit chapter-reference syntax — concepts
    are reached via bare prose, so chapter wins the collision)."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"点估计": "点估计"}
    concept_previews = {
        "点估计": {"href": "/docs/concepts/点估计", "title": "点估计", "summary": "估计量"},
    }
    chapter_previews = {
        "向量函数": {"href": "/docs/chapters/向量函数", "title": "向量函数", "summary": "向量"},
        "点估计": {"href": "/docs/chapters/dian-gu-ji", "title": "点估计", "summary": "ch"},
    }

    body = "参见 [[向量函数]] 一章，以及 [[点估计]] 一章。"
    out = _normalize_concept_links(body, alias_map, concept_previews, chapter_previews)

    # Chapter-only label resolves to the chapter page.
    assert '<PreviewLink href={"/docs/chapters/向量函数"}' in out
    assert ">向量函数</PreviewLink>" in out
    # A label shared by concept + chapter resolves to the chapter (chapter wins the collision).
    assert '<PreviewLink href={"/docs/chapters/dian-gu-ji"}' in out
    assert "/docs/concepts/点估计" not in out


def test_normalize_concept_links_leaves_unknown_chapter_wikilink_bare() -> None:
    from bookwiki.pipeline.nodes import _normalize_concept_links

    chapter_previews = {"别的章": {"href": "x", "title": "别的章", "summary": "s"}}
    out = _normalize_concept_links("见 [[不存在的章]]。", {}, {}, chapter_previews)
    assert "[[不存在的章]]" in out
    assert "<PreviewLink" not in out


def test_normalize_concept_links_never_links_inside_headings() -> None:
    """A ``[[...]]`` wikilink that appears inside a heading (``## ...``) must resolve to plain
    text, never a ``<PreviewLink>``. fumadocs pre-renders heading content into the module-level
    ``toc`` export as bare JSX; a custom component there references an undefined identifier
    (``ReferenceError: PreviewLink is not defined``) and crashes the whole build."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"有界集": "有界集"}
    concept_previews = {
        "有界集": {"href": "/docs/concepts/有界集", "title": "有界集", "summary": "s"},
    }
    chapter_previews = {
        "12.3 Limits and Continuity": {
            "href": "/docs/chapters/ch12/12.3",
            "title": "12.3 Limits and Continuity",
            "summary": "ch",
        },
    }
    body = (
        "## 闭集 $\\neq$ [[有界集]]\n\n"
        "正文里提到[[有界集]]时仍然要链接。\n\n"
        "## 与 [[12.3 Limits and Continuity]] 的联系\n"
    )
    out = _normalize_concept_links(body, alias_map, concept_previews, chapter_previews)

    # Heading lines carry plain text — no JSX component leaks into the toc.
    assert "## 闭集 $\\neq$ 有界集" in out
    assert "## 与 12.3 Limits and Continuity 的联系" in out
    # No PreviewLink on any heading line.
    for line in out.splitlines():
        if line.lstrip().startswith("#"):
            assert "<PreviewLink" not in line
    # Body prose outside headings still links normally.
    assert '<PreviewLink href={"/docs/concepts/有界集"}' in out


def test_auto_link_skips_headings_that_contain_inline_math() -> None:
    """A *bare* concept term in a heading must never be auto-linked — even when the heading also
    contains inline math (``$\\neq$``). The protected-span split fragments such a heading into
    ``## 闭集 `` / ``$\\neq$`` / `` 有界集``; the trailing fragment no longer starts with ``#``, so
    a naive per-line heading guard misses it and injects a ``<PreviewLink>`` into the ``toc`` (the
    real ``ReferenceError: PreviewLink is not defined`` crash on the 闭集 page)."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"有界集": "有界集"}
    concept_previews = {
        "有界集": {"href": "/docs/concepts/有界集", "title": "有界集", "summary": "s"},
    }
    # The heading carries the term as plain prose (no ``[[ ]]``) plus inline math — exactly the
    # generated 闭集 heading. The same term in body prose must still auto-link.
    body = "## 闭集 $\\neq$ 有界集\n\n正文提到有界集这个概念。\n"
    out = _normalize_concept_links(body, alias_map, concept_previews)

    assert "## 闭集 $\\neq$ 有界集" in out
    for line in out.splitlines():
        if line.lstrip().startswith("#"):
            assert "<PreviewLink" not in line
    # Body prose outside the heading still auto-links the term.
    assert '<PreviewLink href={"/docs/concepts/有界集"}' in out.split("\n\n", 1)[1]


def test_normalize_concept_links_suppress_excludes_self_keeps_others() -> None:
    """Concept pages pass auto_link=True with ``suppress`` set to their own name: the page's own
    term is never self-linked, but bare mentions of *other* concepts still auto-link."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"点估计": "点估计", "方差": "方差"}
    concept_previews = {
        "点估计": {"href": "/docs/concepts/点估计", "title": "点估计", "summary": "s"},
        "方差": {"href": "/docs/concepts/方差", "title": "方差", "summary": "v"},
    }

    body = "点估计是一种方法，常用来估计方差。"
    out = _normalize_concept_links(
        body, alias_map, concept_previews, {}, auto_link=True, suppress={"点估计"}
    )

    # The page's own term (点估计) is NOT self-linked.
    assert "/docs/concepts/点估计" not in out
    # A bare mention of another concept (方差) IS auto-linked.
    assert '<PreviewLink href={"/docs/concepts/方差"}' in out


def test_normalize_concept_links_auto_link_false_only_resolves_explicit_wikilinks() -> None:
    """With auto_link=False an explicit [[chapter title]] still resolves to the chapter page, but a
    bare prose mention of a concept term is NOT auto-linked."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"点估计": "点估计"}
    concept_previews = {
        "点估计": {"href": "/docs/concepts/点估计", "title": "点估计", "summary": "s"}
    }
    chapter_previews = {
        "向量函数": {"href": "/docs/chapters/向量函数", "title": "向量函数", "summary": "v"}
    }

    body = "点估计是一种方法，详见 [[向量函数]] 一章。"
    out = _normalize_concept_links(
        body, alias_map, concept_previews, chapter_previews, auto_link=False
    )

    # Explicit chapter wikilink resolves.
    assert '<PreviewLink href={"/docs/chapters/向量函数"}' in out
    # The bare prose mention of the concept "点估计" is left as plain text (not auto-linked).
    assert out.count("<PreviewLink") == 1
    assert "/docs/concepts/点估计" not in out


def test_normalize_concept_links_does_not_auto_link_inside_preview_links() -> None:
    """Auto-linking a shorter term inside an existing PreviewLink would render nested anchors."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {
        "Kirchhoff's Voltage Law (KVL)": "Kirchhoff's Voltage Law (KVL)",
        "Voltage": "Voltage",
    }
    previews = {
        "Kirchhoff's Voltage Law (KVL)": {
            "href": "/docs/concepts/Kirchhoff-s-Voltage-Law-KVL",
            "title": "Kirchhoff's Voltage Law (KVL)",
            "summary": "KVL summary.",
        },
        "Voltage": {
            "href": "/docs/concepts/Voltage",
            "title": "Voltage",
            "summary": "Voltage summary.",
        },
    }

    body = "Use [[Kirchhoff's Voltage Law (KVL)]] for loops. Voltage appears later as plain prose."
    out = _normalize_concept_links(body, alias_map, previews)

    first_link = out.split("</PreviewLink>", 1)[0]
    assert first_link.count("<PreviewLink") == 1
    assert out.count("/docs/concepts/Kirchhoff-s-Voltage-Law-KVL") == 1
    assert out.count("/docs/concepts/Voltage") == 1
