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
        assert payload["_prompt_version"].startswith("v1+")
        assert payload["_agent"].endswith("Agent")
        assert payload["result"]["owner_task_id"].endswith(f":{kind}")


@pytest.mark.asyncio
async def test_concept_pages_node_preserves_unicode_concept_file_names(tmp_path) -> None:
    book_dir = tmp_path / "book"
    concepts_path = book_dir / "work" / "concepts" / "reconciled.json"
    concepts_path.parent.mkdir(parents=True)
    concepts_path.write_text(
        json.dumps(
            {
                "concepts": [
                    {"canonical": "点估计", "aliases": [], "source_chapter_ids": ["chapter-6"]},
                    {"canonical": "矩法估计", "aliases": [], "source_chapter_ids": ["chapter-6"]},
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
        "点估计": "work/agent_results/concepts/点估计.json",
        "矩法估计": "work/agent_results/concepts/矩法估计.json",
    }
    assert (book_dir / outputs["点估计"]).exists()
    assert (book_dir / outputs["矩法估计"]).exists()


def test_integrate_node_writes_fumadocs_mdx_components_and_clears_stale_outputs(
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
    (concept_dir / "点估计.json").write_text(
        json.dumps({"name": "点估计", "body_md": "点估计正文。"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "title": "Point Estimation",
                    "body_md": "正文",
                    "concepts": ["点估计"],
                    "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.summary.json").write_text(
        json.dumps({"result": {"summary_md": "摘要"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [
                        {
                            "question": "点估计是什么?",
                            "choices": ["估计参数", "删除样本"],
                            "answer": "估计参数",
                            "explanation": "点估计给出未知参数的单个估计值。",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                        }
                    ],
                }
            },
            ensure_ascii=False,
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
                            "front": "点估计",
                            "back": "用单个数估计未知参数。",
                            "citations": [{"ref_id": "Week-9-p001", "quote": "source"}],
                        }
                    ],
                }
            },
            ensure_ascii=False,
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
        "concept_pages": {"点估计": "work/agent_results/concepts/点估计.json"},
    }

    result = integrate_node(state, cfg)

    assert result["content_ready"] is True
    assert result["content_index"] == "content/docs/index.mdx"
    assert not (book_dir / "content" / "docs" / "chapters" / "stale.mdx").exists()
    assert not (book_dir / "content" / "docs" / "concepts" / "stale.mdx").exists()
    chapter_page = book_dir / "content" / "docs" / "chapters" / "chapter-6.mdx"
    assert chapter_page.exists()
    chapter_text = chapter_page.read_text(encoding="utf-8")
    assert "<QuizBlock" in chapter_text
    assert "<AnkiDeck" in chapter_text
    assert "```quiz" not in chapter_text
    assert "```card" not in chapter_text
    concept_page = book_dir / "content" / "docs" / "concepts" / "点估计.mdx"
    assert concept_page.exists()
    assert "# 点估计" in concept_page.read_text(encoding="utf-8")
