from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bookwiki.pipeline.nodes import split_node, structure_node
from bookwiki.scheduler.config import default_config
from bookwiki.split.chapter_splitter import (
    parse_approved_structure,
    split_sources_by_structure,
)

APPROVED = """# Mini Book

## ch01 Search Foundations

- 目标: Explain state-space search.
- 范围: textbook p1-p2.
- 来源:
  - textbook-p001

## ch02 Heuristics

- 目标: Explain heuristic search.
- 范围: textbook p3.
- 来源:
  - textbook-p002
"""


def test_parse_approved_structure_extracts_chapters_and_sources() -> None:
    chapters = parse_approved_structure(APPROVED)

    assert [chapter.chapter_id for chapter in chapters] == ["ch01", "ch02"]
    assert chapters[0].title == "Search Foundations"
    assert chapters[0].goal == "Explain state-space search."
    assert chapters[0].scope == "textbook p1-p2."
    assert chapters[0].source_refs == ["textbook-p001"]
    assert chapters[1].source_refs == ["textbook-p002"]


def test_split_sources_by_structure_aligns_fragments_and_writes_appendix(tmp_path: Path) -> None:
    source = tmp_path / "textbook.md"
    source.write_text(
        "# Textbook\n\n"
        "<!-- source_ref: textbook-p001 -->\n\n"
        "States, actions, goals, and search trees.\n\n"
        "<!-- source_ref: textbook-p002 -->\n\n"
        "Heuristics and A star search use estimates.\n\n"
        "<!-- source_ref: textbook-p099 -->\n\n"
        "Administrative appendix material.\n",
        encoding="utf-8",
    )

    result = split_sources_by_structure([source], APPROVED)

    assert "textbook-p001" in result.chapters["ch01"]
    assert "textbook-p002" not in result.chapters["ch01"]
    assert "A star search" in result.chapters["ch02"]
    assert "textbook-p099" in result.chapters["appendix"]
    assert any(
        item["source_ref"] == "textbook-p001"
        and item["chapter_id"] == "ch01"
        and item["confidence"] == 1.0
        for item in result.alignment
    )
    assert "| textbook | 1 | 1 | 1 |" in result.report_md


def test_structure_and_split_nodes_respect_edited_approved_structure(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    source = sources_dir / "textbook.md"
    source.write_text(
        "# Textbook\n\n"
        "<!-- source_ref: textbook-p001 -->\n\n"
        "Introductory search material.\n\n"
        "<!-- source_ref: textbook-p002 -->\n\n"
        "Heuristic search material.\n",
        encoding="utf-8",
    )
    state = {"book_id": cfg.book_id, "sources_md": ["work/sources_md/textbook.md"]}

    structure_state = asyncio.run(structure_node(state, cfg))
    approved_path = cfg.book_dir / structure_state["approved_structure"]
    approved_path.write_text(APPROVED, encoding="utf-8")

    split_state = asyncio.run(split_node({**state, **structure_state}, cfg))

    ch01 = cfg.book_dir / split_state["chapter_sources"]["ch01"]
    ch02 = cfg.book_dir / split_state["chapter_sources"]["ch02"]
    alignment = json.loads(
        (cfg.work_dir / "chapter_sources" / "_alignment.json").read_text(encoding="utf-8")
    )

    assert "Introductory search material" in ch01.read_text(encoding="utf-8")
    assert "Heuristic search material" in ch02.read_text(encoding="utf-8")
    assert alignment["coverage"]["assigned_ratio"] == 1.0
    assert split_state["chapter_titles"]["ch01"] == "Search Foundations"
