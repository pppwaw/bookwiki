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
                "placements": [
                    {"after_block": 0, "item_indexes": [1], "title": "Quick Check"}
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
        '<PreviewLink href={"../concepts/state-space"} title={"state space"} '
        'summary={"State space is the reachable-state set."}>state space</PreviewLink>'
    )
    backlink_preview = (
        '<PreviewLink href={"../chapters/chapter-1"} title={"Chapter 1 Search"} '
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
        description: Book learning home, table of contents, and study tools.
        ---

        # Book

        这页汇总本书的章节目录、核心概念和问答工具。

        ## 目录

        <Cards>
          <Card title={"Chapter 1 Search"} href={"/docs/chapters/chapter-1"} description={"Search summary."} />
        </Cards>

        ## 概念

        - [state space](/docs/concepts/state-space)

        ## 问答

        <ChatBox />
        """
    )
