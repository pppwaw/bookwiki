from __future__ import annotations

import json
import re

import pytest

from bookwiki.pipeline.nodes import (
    _insert_quiz_blocks,
    concept_pages_node,
    generate_node,
    integrate_node,
)
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


def test_quiz_block_heading_is_nested_under_current_section() -> None:
    body_md = (
        "# Chapter 1 Search\n\n"
        "Intro paragraph.\n\n"
        "## Frontier\n\n"
        "Opening explanation.\n\n"
        "### Ordering\n\n"
        "Middle derivation."
    )
    quiz = {
        "items": [
            {
                "question": "What is a frontier?",
                "choices": ["Open nodes", "Closed nodes"],
                "answer": "Open nodes",
                "explanation": "The frontier stores generated nodes.",
            },
            {
                "question": "What changes ordering?",
                "choices": ["Strategy", "Filename"],
                "answer": "Strategy",
                "explanation": "Search strategy orders the frontier.",
            },
        ],
        "placements": [
            {"after_block": 2, "item_indexes": [1], "title": "Checkpoint"},
            {"after_block": 4, "item_indexes": [2], "title": "Practice"},
        ],
    }

    rendered = _insert_quiz_blocks(body_md, quiz)

    assert "### Checkpoint\n\n<QuizBlock>" in rendered
    assert "#### Practice\n\n<QuizBlock>" in rendered
    assert not re.search(r"^## Checkpoint$", rendered, flags=re.MULTILINE)
    assert not re.search(r"^## Practice$", rendered, flags=re.MULTILINE)


@pytest.mark.asyncio
async def test_generate_node_passes_display_chapter_title_to_agents(tmp_path) -> None:
    book_dir = tmp_path / "book"
    source_path = book_dir / "work" / "chapter_sources" / "chapter-6" / "source.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# Chapter 6 Point Estimation\n\n"
        "<!-- source_ref: Week-10-p001 -->\n\n"
        "Point estimation source.",
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "chapter_sources": {"chapter-6": "work/chapter_sources/chapter-6/source.md"},
        "chapter_titles": {"chapter-6": "Point Estimation"},
    }

    result = await generate_node(state, cfg)

    chapter_path = book_dir / result["agent_results"]["chapter-6"]["chapter"]
    payload = json.loads(chapter_path.read_text(encoding="utf-8"))
    assert payload["result"]["title"] == "Chapter 6 Point Estimation"


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
    (concept_dir / "似然函数.json").write_text(
        json.dumps(
            {
                "name": "似然函数",
                "body_md": "似然函数 measures parameter fit.",
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
                        "Opening explanation introduces 似然函数 and point estimation.\n\n"
                        "Middle derivation with \\(\\frac{x}{\\theta}\\).\n\n"
                        "Closing application."
                    ),
                    "concepts": ["Point Estimation", "似然函数"],
                    "citations": [
                        {
                            "ref_id": "Week-9-p001",
                            "quote": (
                                "The moment estimator is \\hat{\\theta}_M = "
                                "\\frac{1}{2n} \\sum X_i^2."
                            ),
                        }
                    ],
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
                            "question": "What does \\(\\hat\\theta\\) estimate?",
                            "choices": ["Estimate \\(\\theta\\)", "Delete samples"],
                            "answer": "Estimate \\(\\theta\\)",
                            "explanation": "It returns a single estimate of \\(\\theta\\).",
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
                            "front": "Point estimation for \\(\\theta\\)",
                            "back": "Estimate an unknown parameter with \\[\\hat\\theta\\].",
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
            "Point Estimation": "work/agent_results/concepts/Point-Estimation.json",
            "似然函数": "work/agent_results/concepts/似然函数.json",
        },
    }

    result = integrate_node(state, cfg)

    assert result["content_ready"] is True
    assert result["content_index"] == "content/docs/index.mdx"
    index_text = (book_dir / "content" / "docs" / "index.mdx").read_text(encoding="utf-8")
    assert index_text.startswith("---\ntitle: Book\n---\n\n# Book")
    assert "[chapters/chapter-6](/docs/chapters/chapter-6)" in index_text
    assert "(./chapters/chapter-6)" not in index_text
    assert not (book_dir / "content" / "docs" / "chapters" / "stale.mdx").exists()
    assert not (book_dir / "content" / "docs" / "concepts" / "stale.mdx").exists()

    chapter_page = book_dir / "content" / "docs" / "chapters" / "chapter-6.mdx"
    assert chapter_page.exists()
    chapter_text = chapter_page.read_text(encoding="utf-8")
    frontmatter = chapter_text.split("---", 2)[1]
    body = chapter_text.split("---", 2)[2]
    assert "title: Chapter 6 Point Estimation" in frontmatter
    assert "summary: Point estimation summary." in frontmatter
    assert "concepts:" in frontmatter
    assert "- Point Estimation" in frontmatter
    assert "- 似然函数" in frontmatter
    assert body.lstrip().startswith("# Chapter 6 Point Estimation")
    assert "## Summary" not in body
    assert "## Concepts" not in body
    assert "# [Point Estimation]" not in body
    assert (
        '<PreviewLink href={"../concepts/似然函数"} title={"似然函数"} '
        'summary={"似然函数 measures parameter fit."}>似然函数</PreviewLink>'
        in body
    )
    assert (
        '<PreviewLink href={"../concepts/Point-Estimation"} title={"Point Estimation"} '
        'summary={"Point estimation may use formulas like $\\\\hat\\\\theta$. '
        'Display math may be written as $$ E(X)=\\\\theta $$"}>point estimation</PreviewLink>'
        in body
    )
    assert "<QuizBlock" in chapter_text
    assert "<QuizItem id=" in chapter_text
    quiz_item_ids = re.findall(r"<QuizItem id=\{\"([^\"]+)\"\}", chapter_text)
    assert quiz_item_ids == ["quiz-001", "quiz-002"]
    assert "<QuizQuestion>" in chapter_text
    assert "<QuizChoices>" in chapter_text
    assert "<QuizChoice id=" in chapter_text
    assert "<QuizCheck />" in chapter_text
    assert "<QuizExplanation>" in chapter_text
    assert "<AnkiDeck" in chapter_text
    assert "cardIds={" in chapter_text
    assert "<AnkiCard id=" in chapter_text
    assert "<AnkiFront>" in chapter_text
    assert "<AnkiBack>" in chapter_text
    assert "items={" not in chapter_text
    assert "cards={" not in chapter_text
    assert '"question":' not in chapter_text
    assert '"front":' not in chapter_text
    assert body.count("<QuizBlock") == 2
    assert body.find("Opening explanation") < body.find("## Checkpoint")
    assert body.find("## Checkpoint") < body.find("Middle derivation")
    assert body.find("Middle derivation") < body.find("## Practice")
    assert body.find("## Practice") < body.find("Closing application.")
    assert body.rfind("<AnkiDeck") > body.rfind("<QuizBlock")
    assert body.rstrip().endswith("</AnkiDeck>")
    assert "```quiz" not in chapter_text
    assert "```card" not in chapter_text
    assert "\\(" not in chapter_text
    assert "\\[" not in chapter_text
    assert "$\\frac{x}{\\theta}$" in chapter_text
    assert "$\\hat\\theta$" in chapter_text
    assert "$\\theta$" in chapter_text
    assert "$$\n\\hat\\theta\n$$" in chapter_text
    assert "The moment estimator is \\hat&#123;\\theta&#125;_M" in chapter_text
    assert "\\frac&#123;1&#125;&#123;2n&#125;" in chapter_text
    assert "<Markdown" not in chapter_text

    concept_page = book_dir / "content" / "docs" / "concepts" / "Point-Estimation.mdx"
    assert concept_page.exists()
    concept_text = concept_page.read_text(encoding="utf-8")
    assert "\\(" not in concept_text
    assert "\\[" not in concept_text
    assert "$\\hat\\theta$" in concept_text
    assert "\n\n$$\nE(X)=\\theta\n$$\n\n" in concept_text
    assert "$\\hat\\theta$" in concept_text
    assert "## Referenced By" in concept_text
    assert (
        '- <PreviewLink href={"../chapters/chapter-6"} '
        'title={"Chapter 6 Point Estimation"} '
        'summary={"Point estimation summary."}>Chapter 6 Point Estimation</PreviewLink>'
        in concept_text
    )

    zh_concept_page = book_dir / "content" / "docs" / "concepts" / "似然函数.mdx"
    assert zh_concept_page.exists()
    zh_concept_text = zh_concept_page.read_text(encoding="utf-8")
    assert (
        '- <PreviewLink href={"../chapters/chapter-6"} '
        'title={"Chapter 6 Point Estimation"} '
        'summary={"Point estimation summary."}>Chapter 6 Point Estimation</PreviewLink>'
        in zh_concept_text
    )
