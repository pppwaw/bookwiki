from __future__ import annotations

import json

import pytest

from bookwiki.pipeline.nodes import concept_pages_node, generate_node, integrate_node
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime


@pytest.mark.asyncio
async def test_generate_node_writes_only_m4_content_agent_outputs(tmp_path) -> None:
    book_dir = tmp_path / "book"
    source_path = book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# Search\n\n<!-- source_ref: source-p001 -->\n\nState space search.",
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
        generation={"quizPerChapter": 2, "cardsPerChapter": 3},
    )
    state = {
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "chapter_titles": {"chapter-1": "Search"},
    }

    result = await generate_node(state, cfg)

    outputs = result["agent_results"]["chapter-1"]
    assert set(outputs) == {"chapter", "summary", "quiz", "card"}
    for kind, rel_path in outputs.items():
        payload = json.loads((book_dir / rel_path).read_text(encoding="utf-8"))
        assert payload["_schema_version"] == "llm.v1"
        assert "_prompt_version" not in payload
        assert payload["_agent"].endswith("Agent")
        assert payload["result"]["owner_task_id"].endswith(f":{kind}")


@pytest.mark.asyncio
async def test_generate_node_requires_chapter_sources(tmp_path) -> None:
    cfg = BookConfig(
        book_dir=tmp_path / "book",
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )

    with pytest.raises(ValueError, match="chapter_sources"):
        await generate_node({"book_id": "book"}, cfg)


@pytest.mark.asyncio
async def test_concept_pages_node_preserves_unicode_concept_file_names(tmp_path) -> None:
    book_dir = tmp_path / "book"
    point_estimation = "\u70b9\u4f30\u8ba1"
    method_of_moments = "\u77e9\u6cd5\u4f30\u8ba1"
    concepts_path = book_dir / "work" / "concepts" / "reconciled.json"
    concepts_path.parent.mkdir(parents=True)
    concepts_path.write_text(
        json.dumps(
            {
                "concepts": [
                    {
                        "canonical": point_estimation,
                        "aliases": [],
                        "source_chapter_ids": ["chapter-6"],
                    },
                    {
                        "canonical": method_of_moments,
                        "aliases": [],
                        "source_chapter_ids": ["chapter-6"],
                    },
                ],
                "alias_map": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )

    result = await concept_pages_node(
        {"reconciled_concepts": "work/concepts/reconciled.json"}, cfg
    )

    outputs = result["concept_pages"]
    assert outputs == {
        point_estimation: f"work/agent_results/concepts/{point_estimation}.json",
        method_of_moments: f"work/agent_results/concepts/{method_of_moments}.json",
    }
    assert (book_dir / outputs[point_estimation]).exists()
    assert (book_dir / outputs[method_of_moments]).exists()


def test_integrate_node_writes_mdx_frontmatter_components_and_concept_backlinks(
    tmp_path,
) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    concept_dir = result_dir / "concepts"
    concept_dir.mkdir(parents=True)
    (book_dir / "content" / "docs" / "chapters").mkdir(parents=True)
    (book_dir / "content" / "docs" / "concepts").mkdir(parents=True)
    (book_dir / "content" / "docs" / "chapters" / "stale.mdx").write_text(
        "stale", encoding="utf-8"
    )
    (book_dir / "content" / "docs" / "concepts" / "stale.mdx").write_text(
        "stale", encoding="utf-8"
    )
    (concept_dir / "Point-Estimation.json").write_text(
        json.dumps(
            {
                "name": "Point Estimation",
                "body_md": (
                    "Point estimation may use formulas like \\(\\hat\\theta\\). "
                    "Display math may be written as \\[E(X)=\\theta\\]."
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "title": "Point Estimation",
                    "body_md": (
                        "Opening explanation.\n\n"
                        "Middle derivation with \\(\\frac{x}{\\theta}\\).\n\n"
                        "Closing application."
                    ),
                    "concepts": ["Point Estimation"],
                    "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.summary.json").write_text(
        json.dumps({"result": {"summary_md": "Point estimation summary."}}),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [
                        {
                            "question": "What is point estimation?",
                            "choices": ["Estimate a parameter", "Delete samples"],
                            "answer": "Estimate a parameter",
                            "explanation": "It returns a single estimate.",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                        },
                        {
                            "question": "Where should the second quiz appear?",
                            "choices": ["Near the application", "Only at the end"],
                            "answer": "Near the application",
                            "explanation": "The model chooses this placement.",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                        }
                    ],
                    "placements": [
                        {"after_block": 0, "item_indexes": [1], "title": "Checkpoint"},
                        {"after_block": 1, "item_indexes": [2], "title": "Practice"},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.card.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [
                        {
                            "front": "Point estimation",
                            "back": "Estimate an unknown parameter with one value.",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")
    state = {
        "agent_results": {
            "chapter-6": {
                "chapter": "work/agent_results/chapter-6.chapter.json",
                "summary": "work/agent_results/chapter-6.summary.json",
                "quiz": "work/agent_results/chapter-6.quiz.json",
                "card": "work/agent_results/chapter-6.card.json",
            }
        },
        "concept_pages": {
            "Point Estimation": "work/agent_results/concepts/Point-Estimation.json"
        },
    }

    result = integrate_node(state, cfg)

    assert result["content_ready"] is True
    assert result["content_index"] == "content/docs/index.mdx"
    index_text = (book_dir / "content" / "docs" / "index.mdx").read_text(encoding="utf-8")
    assert index_text.startswith("---\ntitle: Book\n---\n\n# Book")
    assert not (book_dir / "content" / "docs" / "chapters" / "stale.mdx").exists()
    assert not (book_dir / "content" / "docs" / "concepts" / "stale.mdx").exists()

    chapter_page = book_dir / "content" / "docs" / "chapters" / "chapter-6.mdx"
    assert chapter_page.exists()
    chapter_text = chapter_page.read_text(encoding="utf-8")
    frontmatter = chapter_text.split("---", 2)[1]
    body = chapter_text.split("---", 2)[2]
    assert "summary: Point estimation summary." in frontmatter
    assert "concepts:" in frontmatter
    assert "- Point Estimation" in frontmatter
    assert "## Summary" not in body
    assert "## Concepts" not in body
    assert "<QuizBlock" in chapter_text
    assert "<AnkiDeck" in chapter_text
    assert body.count("<QuizBlock") == 2
    assert body.find("Opening explanation.") < body.find("## Checkpoint")
    assert body.find("## Checkpoint") < body.find("Middle derivation")
    assert body.find("Middle derivation") < body.find("## Practice")
    assert body.find("## Practice") < body.find("Closing application.")
    assert body.rfind("<AnkiDeck") > body.rfind("<QuizBlock")
    assert body.rstrip().endswith("/>")
    assert "```quiz" not in chapter_text
    assert "```card" not in chapter_text
    assert "\\(" not in chapter_text
    assert "$\\frac{x}{\\theta}$" in chapter_text

    concept_page = book_dir / "content" / "docs" / "concepts" / "Point-Estimation.mdx"
    assert concept_page.exists()
    concept_text = concept_page.read_text(encoding="utf-8")
    assert "\\(" not in concept_text
    assert "\\[" not in concept_text
    assert "$\\hat\\theta$" in concept_text
    assert "\n\n$$\nE(X)=\\theta\n$$\n\n" in concept_text
    assert "$\\hat\\theta$" in concept_text
    assert "## Referenced By" in concept_text
    assert "[Point Estimation](../chapters/chapter-6)" in concept_text
