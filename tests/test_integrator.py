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
                "items": [
                    {
                        "question": "What is expanded?",
                        "choices": ["states", "images"],
                        "answer": "states",
                        "explanation": "Search expands states.",
                        "citations": [{"ref_id": "source-p001", "quote": "expands states"}],
                    }
                ],
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

    assert result == {"content_ready": True, "content_index": "content/docs/index.mdx"}
    chapter_mdx = (book_dir / "content" / "docs" / "chapters" / "chapter-1.mdx").read_text(
        encoding="utf-8"
    )
    concept_mdx = (book_dir / "content" / "docs" / "concepts" / "state-space.mdx").read_text(
        encoding="utf-8"
    )
    index_mdx = (book_dir / "content" / "docs" / "index.mdx").read_text(encoding="utf-8")

    chapter_preview = (
        '<PreviewLink href={"/docs/concepts/state-space"} title={"state space"} '
        'summary={"State space is the reachable-state set."}>state space</PreviewLink>'
    )
    backlink_preview = (
        '<PreviewLink href={"/docs/chapters/chapter-1"} title={"Chapter 1 Search"} '
        'summary={"Search summary."}>Chapter 1 Search</PreviewLink>'
    )

    expected_chapter = dedent(
        """\
        ---
        chapter_id: chapter-1
        title: Chapter 1 Search
        type: chapter
        summary: Search summary.
        concepts:
        - state space
        ---

        # Chapter 1 Search

        Core idea CHAPTER_PREVIEW.

        ## Quick Check

        <QuizBlock>
        <QuizItem id={"quiz-001"} answer={"choice-1"} citations={[
          {
            "ref_id": "source-p001",
            "quote": "expands states"
          }
        ]}>
        <QuizQuestion>
        What is expanded?
        </QuizQuestion>
        <QuizChoices>
        <QuizChoice id={"choice-1"}>
        states
        </QuizChoice>
        <QuizChoice id={"choice-2"}>
        images
        </QuizChoice>
        </QuizChoices>
        <QuizCheck />
        <QuizExplanation>
        Search expands states.
        </QuizExplanation>
        </QuizItem>
        </QuizBlock>

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
        ---

        # state space

        State space contains reachable states.

        This second paragraph should stay out of hover previews.

        ## Referenced By

        - BACKLINK_PREVIEW
        """
    ).replace("BACKLINK_PREVIEW", backlink_preview)
    assert concept_mdx == expected_concept
    assert index_mdx == dedent(
        """\
        ---
        title: Book
        description: Book 的互动学习指南：章节目录与核心概念。
        ---

        ## 目录

        <Cards>
          <Card title={"Chapter 1 Search"} href={"/docs/chapters/chapter-1"} description={"Search summary."} />
        </Cards>

        ## 概念

        <Cards>
          <Card title={"state space"} href={"/docs/concepts/state-space"} description={"State space is the reachable-state set."} />
        </Cards>
        """
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

    assert result == {"content_ready": True, "content_index": "content/docs/index.mdx"}
    chapter_mdx = (book_dir / "content" / "docs" / "chapters" / "chapter-1.mdx").read_text(
        encoding="utf-8"
    )

    canonical_b001 = (
        '<BookFigure id="paper-p001-b001" src="/bookwiki-assets/paper-p001-b001.png" '
        'caption="Search tree diagram" />'
    )
    canonical_b002 = (
        '<BookFigure id="paper-p001-b002" src="/bookwiki-assets/paper-p001-b002.png" '
        'caption="Heuristic comparison" />'
    )
    # Inline reference is rewritten to the source-backed canonical tag.
    assert canonical_b001 in chapter_mdx
    assert '<BookFigure id="paper-p001-b001" />' not in chapter_mdx
    # Hallucinated reference (absent from source) is dropped entirely.
    assert "paper-p999-b001" not in chapter_mdx
    # Unreferenced source figure is preserved in a trailing Figures section.
    assert "## Figures" in chapter_mdx
    assert canonical_b002 in chapter_mdx
    assert chapter_mdx.index("## Figures") < chapter_mdx.index("## Sources")
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


def test_normalize_concept_links_resolves_chapter_wikilinks() -> None:
    """A ``[[chapter title]]`` with no matching concept resolves to a chapter PreviewLink;
    a name shared by a concept and a chapter resolves to the concept (concept wins)."""
    from bookwiki.pipeline.nodes import _normalize_concept_links

    alias_map = {"点估计": "点估计"}
    concept_previews = {
        "点估计": {"href": "/docs/concepts/点估计", "title": "点估计", "summary": "估计量"},
    }
    chapter_previews = {
        "向量函数": {"href": "/docs/chapters/向量函数", "title": "向量函数", "summary": "向量"},
        "点估计": {"href": "/docs/chapters/dian-gu-ji", "title": "点估计", "summary": "ch"},
    }

    body = "参见 [[向量函数]] 一章，以及概念 [[点估计]]。"
    out = _normalize_concept_links(body, alias_map, concept_previews, chapter_previews)

    # Chapter-only label resolves to the chapter page.
    assert '<PreviewLink href={"/docs/chapters/向量函数"}' in out
    assert ">向量函数</PreviewLink>" in out
    # A label shared by concept + chapter resolves to the concept (concept wins).
    assert '<PreviewLink href={"/docs/concepts/点估计"}' in out
    assert '/docs/chapters/dian-gu-ji' not in out


def test_normalize_concept_links_leaves_unknown_chapter_wikilink_bare() -> None:
    from bookwiki.pipeline.nodes import _normalize_concept_links

    chapter_previews = {"别的章": {"href": "x", "title": "别的章", "summary": "s"}}
    out = _normalize_concept_links("见 [[不存在的章]]。", {}, {}, chapter_previews)
    assert "[[不存在的章]]" in out
    assert "<PreviewLink" not in out

