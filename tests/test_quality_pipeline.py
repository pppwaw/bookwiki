from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bookwiki.pipeline.nodes import check_node, repair_node
from bookwiki.scheduler.config import BookConfig
from tests.fakes import RecordingRuntime


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _quality_fixture(book_dir: Path) -> dict[str, Any]:
    result_dir = book_dir / "work" / "agent_results"
    concept_dir = result_dir / "concepts"
    _write_text(book_dir / "content" / "docs" / "index.mdx", "---\ntitle: Book\n---\n\n## 目录\n")
    _write_text(
        book_dir / "content" / "docs" / "chapters" / "chapter-1.mdx",
        "---\ntitle: Chapter 1\n---\n\n# Chapter 1\n\n正文。\n\n"
        "<QuizBlock></QuizBlock>\n\n## Anki Cards\n\n## Sources\n",
    )
    _write_text(
        book_dir / "content" / "docs" / "concepts" / "Z-test-z检验.mdx",
        "---\ntitle: Z-test z检验\n---\n\n# Z-test z检验\n\n正文。\n",
    )
    _write_json(
        result_dir / "chapter-1.chapter.json",
        {
            "result": {
                "chapter_id": "chapter-1",
                "title": "Chapter 1",
                "body_md": "随后查得select the cutoff value to control the error rate。",
                "concepts": [],
                "citations": [],
                "owner_task_id": "chapter-1:chapter",
            }
        },
    )
    _write_json(
        result_dir / "chapter-1.summary.json",
        {
            "result": {
                "summary_md": "Summary.",
                "citations": [],
                "owner_task_id": "chapter-1:summary",
            }
        },
    )
    _write_json(
        result_dir / "chapter-1.quiz.json",
        {
            "result": {
                "chapter_id": "chapter-1",
                "items": [
                    {"question": "Q?", "choices": ["A"], "answer": "A", "explanation": "E."}
                ],
                "owner_task_id": "chapter-1:quiz",
            }
        },
    )
    _write_json(
        result_dir / "chapter-1.card.json",
        {
            "result": {
                "chapter_id": "chapter-1",
                "items": [{"front": "F", "back": "B"}],
                "owner_task_id": "chapter-1:card",
            }
        },
    )
    _write_json(
        concept_dir / "Z-test-z检验.json",
        {
            "name": "Z-test z检验",
            "summary_md": "Z 检验。",
            "body_md": "概念页查得select the cutoff value to control the error rate。",
            "related": [],
            "citations": [],
            "owner_task_id": "concept:Z-test z检验",
        },
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
        "concept_pages": {"Z-test z检验": "work/agent_results/concepts/Z-test-z检验.json"},
    }


@pytest.mark.asyncio
async def test_quality_check_node_emits_language_leak_repair_targets(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    state = _quality_fixture(book_dir)
    runtime = RecordingRuntime(
        [
            {
                "owner_task_id": "chapter-1:chapter",
                "findings": [
                    {
                        "category": "language_leak",
                        "quote": "查得select the cutoff value",
                        "explanation": "中英粘连。",
                    }
                ],
            },
            {
                "owner_task_id": "concept-quality:Z-test-z检验",
                "findings": [
                    {
                        "category": "language_leak",
                        "quote": "查得select the cutoff value",
                        "explanation": "中英粘连。",
                    }
                ],
            },
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    cfg.generation["qualityCheck"] = True

    result = await check_node(state, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    quality = [issue for issue in report["issues"] if issue["code"] == "QUALITY_LANGUAGE_LEAK"]
    assert {issue["owner_task_id"] for issue in quality} == {
        "chapter-1:chapter",
        "concept-quality:Z-test-z检验",
    }
    assert all(issue["severity"] == "error" for issue in quality)
    assert "chapter-1:chapter" in result["repair_targets"]
    assert "concept-quality:Z-test-z检验" in result["repair_targets"]


@pytest.mark.asyncio
async def test_quality_check_default_off_makes_zero_llm_calls(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    state = _quality_fixture(book_dir)
    runtime = RecordingRuntime([])
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)

    result = await check_node(state, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    assert [issue for issue in report["issues"] if issue["code"] == "QUALITY_LANGUAGE_LEAK"] == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_quality_check_round_cap_emits_warning_not_repair_target(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    state = _quality_fixture(book_dir)
    state["_repair_rounds"] = {"chapter-1:chapter": 2}
    runtime = RecordingRuntime(
        [
            {
                "owner_task_id": "chapter-1:chapter",
                "findings": [
                    {
                        "category": "language_leak",
                        "quote": "查得select the cutoff value",
                        "explanation": "中英粘连。",
                    }
                ],
            },
            {"owner_task_id": "concept-quality:Z-test-z检验", "findings": []},
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    cfg.generation["qualityCheck"] = True
    cfg.generation["maxQualityRounds"] = 2

    result = await check_node(state, cfg)

    report = json.loads((book_dir / result["check_report"]).read_text(encoding="utf-8"))
    issue = next(issue for issue in report["issues"] if issue["code"] == "QUALITY_LANGUAGE_LEAK")
    assert issue["severity"] == "warning"
    assert "chapter-1:chapter" not in result["repair_targets"]


@pytest.mark.asyncio
async def test_quality_repair_node_routes_chapter_and_concept_rewriters(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    state = _quality_fixture(book_dir)
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "title": "Chapter 1",
                "body_md": "随后查得用于控制错误率的临界值。",
                "concepts": [],
                "citations": [],
                "owner_task_id": "chapter-1:chapter",
            },
            {
                "name": "Z-test z检验",
                "summary_md": "Z 检验。",
                "body_md": "概念页查得用于控制错误率的临界值。",
                "related": [],
                "citations": [],
                "owner_task_id": "concept:Z-test z检验",
            },
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    report = {
        "status": "needs_repair",
        "issues": [
            {
                "severity": "error",
                "code": "QUALITY_LANGUAGE_LEAK",
                "message": "查得select the cutoff value: 中英粘连。",
                "owner_task_id": "chapter-1:chapter",
            },
            {
                "severity": "error",
                "code": "QUALITY_LANGUAGE_LEAK",
                "message": "查得select the cutoff value: 中英粘连。",
                "owner_task_id": "concept-quality:Z-test-z检验",
            },
        ],
    }
    _write_json(book_dir / "work" / "logs" / "check-report.json", report)

    result = await repair_node(
        {
            **state,
            "check_report": "work/logs/check-report.json",
            "repair_targets": ["chapter-1:chapter", "concept-quality:Z-test-z检验"],
        },
        cfg,
    )

    chapter = json.loads(
        (book_dir / "work" / "agent_results" / "chapter-1.chapter.json").read_text()
    )
    concept = json.loads(
        (book_dir / "work" / "agent_results" / "concepts" / "Z-test-z检验.json").read_text()
    )
    assert result["repair_targets"] == []
    assert chapter["_agent"] == "ChapterContentRewriteAgent"
    assert chapter["result"]["body_md"] == "随后查得用于控制错误率的临界值。"
    assert concept["body_md"] == "概念页查得用于控制错误率的临界值。"
