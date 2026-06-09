from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bookwiki.checkers.mdx_validator import validate_mdx
from bookwiki.pipeline.nodes import concept_pages_node
from bookwiki.scheduler.config import BookConfig
from tests.fakes import RecordingRuntime


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _state(book_dir: Path) -> dict[str, Any]:
    _write_json(
        book_dir / "work" / "concepts" / "reconciled.json",
        {
            "concepts": [
                {"canonical": "显著性检验", "aliases": [], "source_chapter_ids": ["chapter-1"]}
            ]
        },
    )
    _write_text(
        book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md",
        "# Source\n\n<!-- source_ref: src-p001 -->\n\ncontent",
    )
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
async def test_concept_pages_inline_repairs_bare_cite_body(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    runtime = RecordingRuntime(
        [
            _concept_response("正文包含 <cite>{X} 这样的裸标签。"),
            _concept_response("正文包含 `cite X` 这样的文本。"),
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)

    result = await concept_pages_node(_state(book_dir), cfg)

    concept_path = book_dir / result["concept_pages"]["显著性检验"]
    concept = json.loads(concept_path.read_text(encoding="utf-8"))
    assert validate_mdx(concept["body_md"]) == []
    assert "`cite X`" in concept["body_md"]
    assert result["concept_generation_issues"] == []


@pytest.mark.asyncio
async def test_concept_pages_inline_exhaustion_surfaces_warning(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    runtime = RecordingRuntime(
        [
            _concept_response("正文包含 <cite>{X} 这样的裸标签。"),
            _concept_response("正文仍包含 <cite>{X} 这样的裸标签。"),
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    cfg.generation["maxRepairRounds"] = 1

    result = await concept_pages_node(_state(book_dir), cfg)

    warnings = result["concept_generation_issues"]
    assert warnings[0]["code"] == "CONCEPT_VALIDATION_UNRESOLVED"
    assert warnings[0]["severity"] == "warning"
    concept_path = book_dir / result["concept_pages"]["显著性检验"]
    concept = json.loads(concept_path.read_text(encoding="utf-8"))
    assert "<cite>{X}" in concept["body_md"]
