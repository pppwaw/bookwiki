from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bookwiki.generate.sections import generate_chapter_sections
from bookwiki.pipeline.nodes import check_node, concept_pages_node
from bookwiki.scheduler.config import BookConfig
from tests.fakes import RecordingRuntime

SOURCE_MD = "# Search\n\n<!-- source_ref: src-p001 -->\n\nState space search content."


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _check_fixture(book_dir: Path) -> dict[str, Any]:
    result_dir = book_dir / "work" / "agent_results"
    _write_text(book_dir / "content" / "docs" / "index.mdx", "---\ntitle: Book\n---\n")
    _write_text(
        book_dir / "content" / "docs" / "chapters" / "chapter-1.mdx",
        "---\ntitle: Chapter 1\n---\n\n# Chapter 1\n\n正文。\n\n"
        "<QuizBlock></QuizBlock>\n\n## Anki Cards\n\n## Sources\n",
    )
    _write_json(
        result_dir / "chapter-1.chapter.json",
        {
            "result": {
                "chapter_id": "chapter-1",
                "title": "Chapter 1",
                "body_md": "随后查得select the cutoff value。",
                "concepts": [],
                "citations": [],
                "owner_task_id": "chapter-1:chapter",
            }
        },
    )
    _write_json(result_dir / "chapter-1.summary.json", {"result": {"citations": []}})
    _write_json(
        result_dir / "chapter-1.quiz.json",
        {"result": {"items": [], "owner_task_id": "chapter-1:quiz"}},
    )
    _write_json(
        result_dir / "chapter-1.card.json",
        {"result": {"items": [], "owner_task_id": "chapter-1:card"}},
    )
    return {
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        },
        "concept_pages": {},
    }


def _plan_response() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "sections": [
            {
                "chapter_id": "chapter-1",
                "index": 0,
                "title": "S0",
                "topics_covered": ["t0"],
                "concepts_introduced": [],
                "learning_goal": "goal",
            }
        ],
        "owner_task_id": "chapter-1:section_plan",
    }


def _section_response(body_md: str) -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "section_index": 0,
        "title": "S0",
        "body_md": body_md,
        "concepts": [],
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
        "figure_requests": [],
        "owner_task_id": "chapter-1:section:000",
    }


def _chapter_response(body_md: str) -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "title": "Search",
        "body_md": body_md,
        "concepts": [],
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
        "owner_task_id": "chapter-1:chapter",
    }


def _quality_report(findings: list[dict[str, str]]) -> dict[str, Any]:
    return {"owner_task_id": "inline", "findings": findings}


def _application_quiz_response() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "items": [],
        "placements": [],
        "owner_task_id": "chapter-1:quiz",
    }


def _card_response() -> dict[str, Any]:
    return {"chapter_id": "chapter-1", "items": [], "owner_task_id": "chapter-1:card"}


def _summary_response() -> dict[str, Any]:
    return {
        "chapter_id": "chapter-1",
        "summary_md": "Summary.",
        "key_points": [],
        "citations": [],
        "owner_task_id": "chapter-1:summary",
    }


@pytest.mark.asyncio
async def test_quality_check_macro_default_off_makes_zero_llm_calls(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    runtime = RecordingRuntime([])
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)

    result = await check_node(_check_fixture(book_dir), cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    assert [issue for issue in report["issues"] if issue["code"] == "QUALITY_LANGUAGE_LEAK"] == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_quality_check_macro_enabled_still_makes_zero_llm_calls(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    runtime = RecordingRuntime([])
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    cfg.generation["qualityCheck"] = True

    result = await check_node(_check_fixture(book_dir), cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    assert [issue for issue in report["issues"] if issue["code"] == "QUALITY_LANGUAGE_LEAK"] == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_generate_inline_quality_rewrites_language_leak(tmp_path: Path) -> None:
    runtime = RecordingRuntime(
        [
            _plan_response(),
            _section_response("随后查得select the cutoff value来控制错误率。"),
            _quality_report(
                [
                    {
                        "category": "language_leak",
                        "quote": "查得select the cutoff value",
                        "explanation": "中英粘连。",
                    }
                ]
            ),
            _chapter_response("# Search\n\n## S0\n\n随后查得用于选择临界值来控制错误率。"),
            _quality_report([]),
            _application_quiz_response(),
            _card_response(),
            _summary_response(),
        ]
    )
    cfg = BookConfig(
        book_dir=tmp_path / "book",
        book_id="book",
        title="Book",
        llm_runtime=runtime,
        generation={"qualityCheck": True},
    )

    result = await generate_chapter_sections(
        cfg=cfg,
        chapter_id="chapter-1",
        title="Search",
        source_md=SOURCE_MD,
        source_path="work/chapter_sources/chapter-1/source.md",
        topics=["t0"],
        figures=[],
        skeleton_payload={},
    )

    assert "查得用于选择临界值" in result.chapter.body_md
    assert "select the cutoff value" not in result.chapter.body_md


def _concept_state(book_dir: Path) -> dict[str, Any]:
    _write_json(
        book_dir / "work" / "concepts" / "reconciled.json",
        {
            "concepts": [
                {"canonical": "显著性检验", "aliases": [], "source_chapter_ids": ["chapter-1"]}
            ]
        },
    )
    _write_text(book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md", SOURCE_MD)
    return {
        "reconciled_concepts": "work/concepts/reconciled.json",
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "agent_results": {},
    }


def _concept_response(body_md: str) -> dict[str, Any]:
    return {
        "name": "显著性检验",
        "summary_md": "概念摘要。",
        "body_md": body_md,
        "related": [],
        "citations": [{"ref_id": "src-p001", "quote": "content"}],
        "owner_task_id": "concept:显著性检验",
    }


@pytest.mark.asyncio
async def test_concept_inline_quality_rewrites_language_leak(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    runtime = RecordingRuntime(
        [
            _concept_response("概念页查得select the cutoff value来控制错误率。"),
            _quality_report(
                [
                    {
                        "category": "language_leak",
                        "quote": "查得select the cutoff value",
                        "explanation": "中英粘连。",
                    }
                ]
            ),
            _concept_response("概念页查得用于选择临界值来控制错误率。"),
            _quality_report([]),
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    cfg.generation["qualityCheck"] = True

    result = await concept_pages_node(_concept_state(book_dir), cfg)

    concept_path = book_dir / result["concept_pages"]["显著性检验"]
    concept = json.loads(concept_path.read_text(encoding="utf-8"))
    assert "查得用于选择临界值" in concept["body_md"]
    assert "select the cutoff value" not in concept["body_md"]
