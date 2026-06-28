from __future__ import annotations

import json
import re

import pytest

from bookwiki.pipeline.nodes import (
    _resolve_item_slots,
    concept_pages_node,
    generate_node,
    integrate_node,
)
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from tests.fakes import RecordingRuntime


def _section_plan_response() -> dict[str, object]:
    return {
        "chapter_id": "chapter-1",
        "sections": [
            {
                "chapter_id": "chapter-1",
                "index": 0,
                "title": "State Space Search",
                "topics_covered": ["State space search"],
                "concepts_introduced": ["search"],
                "learning_goal": "Understand state space search.",
            }
        ],
        "owner_task_id": "chapter-1:section_plan",
    }


def _section_response() -> dict[str, object]:
    citation = {"ref_id": "paper-p001", "quote": "search"}
    return {
        "chapter_id": "chapter-1",
        "section_index": 0,
        "title": "State Space Search",
        "body_md": "State space search expands a frontier of nodes.",
        "concepts": ["search"],
        "citations": [citation],
        "figure_requests": [],
        "owner_task_id": "chapter-1:section:000",
    }


def _card_response() -> dict[str, object]:
    return {
        "chapter_id": "chapter-1",
        "items": [],
        "owner_task_id": "chapter-1:card",
    }


def _summary_response() -> dict[str, object]:
    return {
        "chapter_id": "chapter-1",
        "summary_md": "Search summary.",
        "key_points": ["Search expands a frontier."],
        "citations": [],
        "owner_task_id": "chapter-1:summary",
    }


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
        "Middle derivation.\n\n"
        '<QuizBlock>\n<QuizItemSlot id="chapter-1:s0:slot-000" '
        'topic="t" sourceRefs={["p1"]} />\n</QuizBlock>'
    )
    quiz = {
        "items": [
            {
                "slot_id": "chapter-1:s0:slot-000",
                "question": "What is a frontier?",
                "choices": ["Open nodes", "Closed nodes"],
                "answer": "Open nodes",
                "explanation": "The frontier stores generated nodes.",
            }
        ],
    }

    rendered = _resolve_item_slots(body_md, quiz)

    assert "<QuizBlock>" in rendered
    assert "<QuizItem " in rendered
    assert "chapter-1:s0:slot-000" not in rendered  # slot replaced by the filled item


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
    # The display title is now the verbatim chapter title (no synthesised "Chapter N" prefix).
    assert payload["result"]["title"] == "Point Estimation"


@pytest.mark.asyncio
async def test_generate_node_feeds_topics_to_planner_and_figures_to_section(tmp_path) -> None:
    book_dir = tmp_path / "book"
    source_path = book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# Search\n\n"
        "<!-- source_ref: paper-p001 -->\n\n"
        "Intro about search.\n\n"
        '<BookFigure id="paper-p001-b001" sourceRef="paper-p001" '
        'src="/bookwiki-assets/paper/a.png" caption="Search tree diagram" />\n',
        encoding="utf-8",
    )
    runtime = RecordingRuntime(
        [
            _section_plan_response(),
            _section_response(),
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=runtime,
    )
    state = {
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "chapter_titles": {"chapter-1": "Search"},
        "chapter_topics": {"chapter-1": ["State space search"]},
    }

    await generate_node(state, cfg)

    # The section planner receives the approved-structure topics.
    planner_prompt = runtime.calls[0]["user"]
    assert '"topics"' in planner_prompt
    assert "State space search" in planner_prompt
    # The section agent receives the figures parsed from the chapter source.
    section_prompt = runtime.calls[1]["user"]
    assert '"figures"' in section_prompt
    assert "paper-p001-b001" in section_prompt
    assert "Search tree diagram" in section_prompt
    # Option B isolation: the summary agent must not receive figures payloads.
    summary_prompt = runtime.calls[-1]["user"]
    assert '"figures"' not in summary_prompt


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

    result = await concept_pages_node({"reconciled_concepts": "work/concepts/reconciled.json"}, cfg)

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
    docs = book_dir / "site" / "content" / "docs"
    (docs / "chapters").mkdir(parents=True)
    (docs / "concepts").mkdir(parents=True)
    (docs / "chapters" / "stale.mdx").write_text("stale", encoding="utf-8")
    (docs / "concepts" / "stale.mdx").write_text("stale", encoding="utf-8")
    (concept_dir / "Point-Estimation.json").write_text(
        json.dumps(
            {
                "name": "Point Estimation",
                "body_md": (
                    "Point estimation may use formulas like $\\hat\\theta$. "
                    "Display math may be written as $$E(X)=\\theta$$."
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
                        "## Checkpoint\n\n"
                        '<QuizBlock>\n<QuizItemSlot id="chapter-6:s0:slot-000" '
                        'topic="t" sourceRefs={["Week-9-p001"]} />\n</QuizBlock>\n\n'
                        "Middle derivation with $\\frac{x}{\\theta}$.\n\n"
                        "## Practice\n\n"
                        '<QuizBlock>\n<QuizItemSlot id="chapter-6:s0:slot-001" '
                        'topic="t" sourceRefs={["Week-9-p001"]} />\n</QuizBlock>\n\n'
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
                            "question": "What does $\\hat\\theta$ estimate?",
                            "choices": ["Estimate $\\theta$", "Delete samples"],
                            "answer": "Estimate $\\theta$",
                            "explanation": "It returns a single estimate of $\\theta$.",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                            "slot_id": "chapter-6:s0:slot-000",
                        },
                        {
                            "question": "Where should the second quiz appear?",
                            "choices": ["Near the application", "Only at the end"],
                            "answer": "Near the application",
                            "explanation": "The model chooses this placement.",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                            "slot_id": "chapter-6:s0:slot-001",
                        },
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
                            "front": "Point estimation for $\\theta$",
                            "back": "Estimate an unknown parameter with $$\\hat\\theta$$.",
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
    assert result["content_index"] == "site/content/docs/index.mdx"
    index_text = (book_dir / "site" / "content" / "docs" / "index.mdx").read_text(encoding="utf-8")
    assert index_text.startswith(
        "---\ntitle: Book\ndescription: Book 的互动学习指南：章节目录与核心概念。\n---\n\n## 目录"
    )
    assert (
        '<Card title={"Point Estimation"} href={"/docs/chapters/chapter-6"} '
        'description={"Point estimation summary."} />'
    ) in index_text
    assert (
        '<Card title={"Point Estimation"} href={"/docs/concepts/Point-Estimation"}'
    ) in index_text
    assert "(./chapters/chapter-6)" not in index_text
    assert not (book_dir / "site" / "content" / "docs" / "chapters" / "stale.mdx").exists()
    assert not (book_dir / "site" / "content" / "docs" / "concepts" / "stale.mdx").exists()

    chapter_page = book_dir / "site" / "content" / "docs" / "chapters" / "chapter-6.mdx"
    assert chapter_page.exists()
    chapter_text = chapter_page.read_text(encoding="utf-8")
    frontmatter = chapter_text.split("---", 2)[1]
    body = chapter_text.split("---", 2)[2]
    assert "title: Point Estimation" in frontmatter
    assert "summary: Point estimation summary." in frontmatter
    assert "concepts:" in frontmatter
    assert "- Point Estimation" in frontmatter
    assert "- 似然函数" in frontmatter
    assert body.lstrip().startswith("# Point Estimation")
    assert "## Summary" not in body
    assert "## Concepts" not in body
    assert "# [Point Estimation]" not in body
    assert (
        '<PreviewLink href={"/docs/concepts/似然函数"} title={"似然函数"} '
        'summary={"似然函数 measures parameter fit."}>似然函数</PreviewLink>' in body
    )
    assert (
        '<PreviewLink href={"/docs/concepts/Point-Estimation"} title={"Point Estimation"} '
        'summary={"Point estimation may use formulas like $\\\\hat\\\\theta$. '
        'Display math may be written as $$E(X)=\\\\theta$$."}>point estimation</PreviewLink>'
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
    assert "$$\\hat\\theta$$" in chapter_text
    assert "The moment estimator is \\hat&#123;\\theta&#125;_M" in chapter_text
    assert "\\frac&#123;1&#125;&#123;2n&#125;" in chapter_text
    assert "<Markdown" not in chapter_text

    concept_page = book_dir / "site" / "content" / "docs" / "concepts" / "Point-Estimation.mdx"
    assert concept_page.exists()
    concept_text = concept_page.read_text(encoding="utf-8")
    assert "\\(" not in concept_text
    assert "\\[" not in concept_text
    assert "$\\hat\\theta$" in concept_text
    assert "$$E(X)=\\theta$$" in concept_text
    assert "$\\hat\\theta$" in concept_text
    assert "## Referenced By" in concept_text
    assert (
        '- <PreviewLink href={"/docs/chapters/chapter-6"} '
        'title={"Point Estimation"} '
        'summary={"Point estimation summary."}>Point Estimation</PreviewLink>'
        in concept_text
    )

    zh_concept_page = book_dir / "site" / "content" / "docs" / "concepts" / "似然函数.mdx"
    assert zh_concept_page.exists()
    zh_concept_text = zh_concept_page.read_text(encoding="utf-8")
    assert (
        '- <PreviewLink href={"/docs/chapters/chapter-6"} '
        'title={"Point Estimation"} '
        'summary={"Point estimation summary."}>Point Estimation</PreviewLink>'
        in zh_concept_text
    )


def _write_leaf_agent_results(result_dir, ch_id: str, title: str) -> dict[str, str]:
    """Write minimal chapter/summary/quiz/card JSON for one leaf chapter."""
    (result_dir / f"{ch_id}.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": ch_id,
                    "title": title,
                    "body_md": f"{title} body explanation.",
                    "concepts": [],
                    "citations": [],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / f"{ch_id}.summary.json").write_text(
        json.dumps({"result": {"summary_md": f"{title} summary."}}),
        encoding="utf-8",
    )
    (result_dir / f"{ch_id}.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": ch_id,
                    "items": [],
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / f"{ch_id}.card.json").write_text(
        json.dumps({"result": {"chapter_id": ch_id, "items": [{"front": "F", "back": "B"}]}}),
        encoding="utf-8",
    )
    return {
        "chapter": f"work/agent_results/{ch_id}.chapter.json",
        "summary": f"work/agent_results/{ch_id}.summary.json",
        "quiz": f"work/agent_results/{ch_id}.quiz.json",
        "card": f"work/agent_results/{ch_id}.card.json",
    }


def test_integrate_node_writes_two_level_grouped_chapters(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    result_dir.mkdir(parents=True)
    chapters_root = book_dir / "site" / "content" / "docs" / "chapters"
    chapters_root.mkdir(parents=True)
    # Stale flat file from a previous (ungrouped) run must be cleared on rerun.
    (chapters_root / "chapter-9-2.mdx").write_text("stale", encoding="utf-8")

    state = {
        "agent_results": {
            "chapter-9-2": _write_leaf_agent_results(
                result_dir, "chapter-9-2", "9.2 Infinite Series"
            ),
            "chapter-9-5": _write_leaf_agent_results(
                result_dir, "chapter-9-5", "9.5 Alternating Series"
            ),
        },
        "chapter_groups": {
            "chapter-9": {
                "title": "Chapter 9 Infinite Series",
                "leaf_ids": ["chapter-9-2", "chapter-9-5"],
            }
        },
        "concept_pages": {},
    }
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    integrate_node(state, cfg)

    # Leaves live nested under the group folder, not flat.
    assert (chapters_root / "chapter-9" / "chapter-9-2.mdx").exists()
    assert (chapters_root / "chapter-9" / "chapter-9-5.mdx").exists()
    assert not (chapters_root / "chapter-9-2.mdx").exists()

    group_meta = json.loads((chapters_root / "chapter-9" / "meta.json").read_text("utf-8"))
    assert group_meta["title"] == "Chapter 9 Infinite Series"
    assert group_meta["pages"] == ["chapter-9-2", "chapter-9-5"]

    top_meta = json.loads((chapters_root / "meta.json").read_text("utf-8"))
    assert top_meta["pages"] == ["chapter-9"]

    leaf_text = (chapters_root / "chapter-9" / "chapter-9-2.mdx").read_text("utf-8")
    assert "chapter_id: chapter-9-2" in leaf_text
    assert leaf_text.split("---", 2)[2].lstrip().startswith("# 9.2 Infinite Series")

    index_text = (book_dir / "site" / "content" / "docs" / "index.mdx").read_text("utf-8")
    assert 'href={"/docs/chapters/chapter-9/chapter-9-2"}' in index_text
