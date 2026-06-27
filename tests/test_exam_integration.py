from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bookwiki.agents.source_summary_agent import SourceSummaryAgent
from bookwiki.pipeline.nodes import _chapter_exam_pool, _exam_chapter_ids, integrate_node
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _agent(result: dict[str, Any]) -> dict[str, Any]:
    return {"_schema_version": "llm.v1", "_agent": "Fixture", "_model": "stub", "result": result}


def _seed_chapter(result_dir: Path, owner_suffix: str) -> None:
    _write_json(
        result_dir / "chapter-1.chapter.json",
        _agent(
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nBody.",
                "concepts": [],
                "citations": [],
                "owner_task_id": "chapter-1:chapter",
            }
        ),
    )
    _write_json(
        result_dir / "chapter-1.summary.json",
        _agent(
            {
                "chapter_id": "chapter-1",
                "summary_md": "Summary.",
                "key_points": [],
                "citations": [],
                "owner_task_id": "chapter-1:summary",
            }
        ),
    )
    _write_json(
        result_dir / "chapter-1.quiz.json",
        _agent({"chapter_id": "chapter-1", "items": [], "owner_task_id": "chapter-1:quiz"}),
    )
    _write_json(
        result_dir / "chapter-1.card.json",
        _agent({"chapter_id": "chapter-1", "items": [], "owner_task_id": "chapter-1:card"}),
    )
    _write_json(
        result_dir / "chapter-1.exam.json",
        _agent(
            {
                "chapter_id": "chapter-1",
                "owner_task_id": f"chapter-1:{owner_suffix}",
                "questions": [
                    {
                        "type": "single_choice",
                        "id": "ex-1",
                        "question": "What does search expand?",
                        "options": ["states", "pixels"],
                        "answer": ["states"],
                        "explanation": "It expands states.",
                        "source_refs": [],
                    }
                ],
            }
        ),
    )


def _state() -> dict[str, Any]:
    return {
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
                "exam": "work/agent_results/chapter-1.exam.json",
            }
        },
        "concept_pages": {},
    }


def test_chapter_with_exam_becomes_folder_with_exam_page(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    _seed_chapter(book_dir / "work" / "agent_results", "exam")
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    integrate_node(_state(), cfg)

    chapters = book_dir / "content" / "docs" / "chapters"
    assert (chapters / "chapter-1" / "index.mdx").exists()
    assert not (chapters / "chapter-1.mdx").exists()
    exam_mdx = (chapters / "chapter-1" / "exam.mdx").read_text(encoding="utf-8")
    assert '<ExamBlock chapterId="chapter-1" mode="exam">' in exam_mdx
    meta = json.loads((chapters / "chapter-1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["pages"] == ["index", "exam"]


def test_walkthrough_mode_detected_from_owner_task_id(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    _seed_chapter(book_dir / "work" / "agent_results", "explain")
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")

    integrate_node(_state(), cfg)

    exam_mdx = (
        book_dir / "content" / "docs" / "chapters" / "chapter-1" / "exam.mdx"
    ).read_text(encoding="utf-8")
    assert 'mode="walkthrough"' in exam_mdx


def test_exam_chapter_ids_detects_by_title_keyword() -> None:
    state = {
        "chapter_sources": {"ch-1": "work/chapter_sources/ch-1", "midterm": "work/cs/midterm"},
        "chapter_titles": {"ch-1": "Search Basics", "midterm": "期中试卷"},
    }

    assert _exam_chapter_ids(state) == {"midterm"}


@pytest.mark.asyncio
async def test_source_summary_detects_exam_and_splits_questions() -> None:
    body = (
        "# 期中试卷\n<!-- source_ref: mid-p001 -->\n"
        "1. 计算 f(x,y)=x^2+y^2 的梯度并说明方向导数。\n"
        "2. 证明 A* 在可采纳启发式下是最优的。\n"
    )

    result = await SourceSummaryAgent().run(
        {"span_text": body, "source_id": "midterm"}, model="stub", runtime=TestLLMRuntime()
    )

    assert result.is_exam is True
    assert len(result.exam_questions) == 2


def test_chapter_exam_pool_maps_questions_by_topic(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    _write_json(
        book_dir / "work" / "structure" / "exam-pool.json",
        {
            "questions": [
                {"question": "Gradient of f.", "concepts": ["gradient"], "source_refs": ["m-p001"]},
                {"question": "Resolution in CNF.", "concepts": ["resolution"], "source_refs": []},
            ]
        },
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book")
    state = {
        "chapter_topics": {
            "ch-calc": ["gradient", "partial derivative"],
            "ch-logic": ["resolution"],
        }
    }

    calc_pool = _chapter_exam_pool(state, cfg, "ch-calc")
    logic_pool = _chapter_exam_pool(state, cfg, "ch-logic")

    assert [q["question"] for q in calc_pool] == ["Gradient of f."]
    assert [q["question"] for q in logic_pool] == ["Resolution in CNF."]
