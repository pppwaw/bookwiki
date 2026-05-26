from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

from bookwiki.pipeline.nodes import integrate_node
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
            "body_md": "State space contains reachable states.",
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

    assert chapter_mdx == dedent(
        """\
        ---
        chapter_id: chapter-1
        title: Search
        type: chapter
        summary: Search summary.
        concepts:
        - state space
        ---

        # Search

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

        Core idea [state space](../concepts/state-space).

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
    )
    assert concept_mdx == dedent(
        """\
        ---
        title: state space
        type: concept
        ---

        # state space

        State space contains reachable states.

        ## Referenced By

        - [Search](../chapters/chapter-1)
        """
    )
    assert index_mdx == dedent(
        """\
        ---
        title: Book
        ---

        # Book

        - [chapters/chapter-1](/docs/chapters/chapter-1)
        """
    )
