from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from bookwiki.agents.source_summary_agent import SourceSummaryAgent
from bookwiki.agents.structure_agent import StructureAgent
from bookwiki.pipeline.nodes import split_node, structure_node
from bookwiki.scheduler.config import default_config
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.split.chapter_splitter import (
    parse_approved_structure,
    split_sources_by_structure,
)

APPROVED = """chapters:
  - title: Chapter 1 Search Foundations
    topics:
      - State space search
    source_refs:
      - textbook-p001
  - title: Chapter 2 Heuristics
    topics:
      - Heuristic search
    source_refs:
      - textbook-p002
"""

APPROVED_V2 = """chapters:
  - title: Chapter 6 Point Estimation
    topics:
      - Method of moments
      - Maximum likelihood estimation
    source_refs:
      - Week-9-p001
      - Week-10-p001
"""


def test_parse_approved_structure_extracts_chapters_and_sources() -> None:
    chapters = parse_approved_structure(APPROVED)

    assert [chapter.chapter_id for chapter in chapters] == ["chapter-1", "chapter-2"]
    assert chapters[0].title == "Search Foundations"
    assert chapters[0].topics == ["State space search"]
    assert chapters[0].source_refs == ["textbook-p001"]
    assert chapters[1].source_refs == ["textbook-p002"]


def test_parse_approved_structure_rejects_legacy_markdown_bullet_format() -> None:
    legacy = (
        "# Mini Book\n\n"
        "## Chapter 6 Point Estimation\n\n"
        "- Target: Explain estimators.\n"
        "- Scope: Week 9 and Week 10.\n"
        "- Sources:\n"
        "  - Week-9-p001\n"
    )

    try:
        parse_approved_structure(legacy)
    except ValueError as exc:
        assert "top-level chapters list" in str(exc)
    else:
        raise AssertionError("legacy Markdown bullet format should be rejected")


def test_parse_approved_structure_rejects_legacy_markdown_section_format() -> None:
    legacy_sections = """# Proposed Structure

## Chapter 6 Point Estimation

### Goal
Explain estimators.

### Scope
Week 10.

### Topics
- Method of moments

### Source refs
- `Week-10-p001`
"""

    try:
        parse_approved_structure(legacy_sections)
    except ValueError as exc:
        assert "top-level chapters list" in str(exc)
    else:
        raise AssertionError("section-based Markdown structure format should be rejected")


def test_parse_approved_structure_rejects_legacy_chapter_ids() -> None:
    legacy = APPROVED.replace("Chapter 1 Search Foundations", "ch01 Search Foundations")

    try:
        parse_approved_structure(legacy)
    except ValueError as exc:
        assert "chapter titles" in str(exc)
    else:
        raise AssertionError("legacy chNN headings should be rejected")


def test_parse_approved_structure_accepts_yaml_contract() -> None:
    chapters = parse_approved_structure(APPROVED_V2)

    assert len(chapters) == 1
    assert chapters[0].chapter_id == "chapter-6"
    assert chapters[0].title == "Point Estimation"
    assert chapters[0].topics == ["Method of moments", "Maximum likelihood estimation"]
    assert chapters[0].source_refs == ["Week-9-p001", "Week-10-p001"]


def test_structure_agent_uses_detected_chapter_numbers_from_source_titles(tmp_path: Path) -> None:
    source = tmp_path / "Week-10.md"
    source.write_text(
        "# Week-10\n\n"
        "<!-- source_ref: Week-10-p001 -->\n\n"
        "# Chapter 6 The point estimation (bad ocr)\n\n"
        "The method of moments and maximum likelihood estimation.",
        encoding="utf-8",
    )

    runtime = TestLLMRuntime()
    summary = asyncio.run(SourceSummaryAgent().run(source, model="stub", runtime=runtime))
    result = asyncio.run(
        StructureAgent().run(
            {"summaries": [summary.model_dump(mode="json")]}, model="stub", runtime=runtime
        )
    )

    structure = yaml.safe_load(result.proposed_structure_yaml)

    assert summary.detected_chapter_id == "ch06"
    assert summary.detected_title == "Point Estimation"
    assert structure["chapters"][0]["title"] == "Chapter 6 Point Estimation"
    assert structure["chapters"][0]["source_refs"] == ["Week-10-p001"]
    assert "goal" not in structure["chapters"][0]
    assert "scope" not in structure["chapters"][0]
    assert "evidence" not in structure["chapters"][0]
    assert "ch06" not in result.proposed_structure_yaml
    assert "ch02" not in result.proposed_structure_yaml


def test_structure_agent_reflects_source_content_in_chapter_plan(tmp_path: Path) -> None:
    source = tmp_path / "Week-10.md"
    source.write_text(
        "# Week-10\n\n"
        "<!-- source_ref: Week-10-p001 -->\n\n"
        "# Chapter 6 The point estimation\n\n"
        "Point estimation introduces the method of moments and maximum likelihood "
        "estimation for unknown parameters.\n\n"
        "# More general case\n\n"
        "Moment estimators equate sample moments with population moments.",
        encoding="utf-8",
    )

    runtime = TestLLMRuntime()
    summary = asyncio.run(SourceSummaryAgent().run(source, model="stub", runtime=runtime))
    result = asyncio.run(
        StructureAgent().run(
            {"summaries": [summary.model_dump(mode="json")]}, model="stub", runtime=runtime
        )
    )

    assert "Week-10" not in summary.headings
    assert "method of moments" in result.proposed_structure_yaml
    assert "maximum likelihood estimation" in result.proposed_structure_yaml
    assert "More general case" in result.proposed_structure_yaml
    assert "Chapter 2 Practice" not in result.proposed_structure_yaml
    assert "Cover the source material assigned" not in result.proposed_structure_yaml
    assert "Automatically grouped source set" not in result.proposed_structure_yaml


def test_structure_agent_merges_sources_with_same_detected_chapter() -> None:
    result = asyncio.run(
        StructureAgent().run(
            {
                "summaries": [
                    {
                        "source_id": "Week-9",
                        "source_refs": ["Week-9-p001"],
                        "detected_chapter_id": "ch06",
                        "detected_title": "Point Estimation",
                    },
                    {
                        "source_id": "Week-10",
                        "source_refs": ["Week-10-p001"],
                        "detected_chapter_id": "ch06",
                        "detected_title": "Point Estimation",
                    },
                ]
            },
            model="stub",
            runtime=TestLLMRuntime(),
        )
    )

    structure = yaml.safe_load(result.proposed_structure_yaml)

    assert [chapter["title"] for chapter in structure["chapters"]] == [
        "Chapter 6 Point Estimation"
    ]
    assert "ch06 Point Estimation" not in result.proposed_structure_yaml
    assert "Chapter 2 Practice" not in result.proposed_structure_yaml
    assert structure["chapters"][0]["source_refs"] == ["Week-9-p001", "Week-10-p001"]


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

    assert "textbook-p001" in result.chapters["chapter-1"]
    assert "textbook-p002" not in result.chapters["chapter-1"]
    assert "A star search" in result.chapters["chapter-2"]
    assert "textbook-p099" in result.chapters["appendix"]
    assert any(
        item["source_ref"] == "textbook-p001"
        and item["chapter_id"] == "chapter-1"
        and item["confidence"] == 1.0
        for item in result.alignment
    )
    assert "| textbook | 1 | 1 | 1 |" in result.report_md


def test_structure_and_split_nodes_respect_edited_approved_structure(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = TestLLMRuntime()
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
    stale = cfg.work_dir / "chapter_sources" / "ch99" / "source.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    structure_state = asyncio.run(structure_node(state, cfg))
    approved_path = cfg.book_dir / structure_state["approved_structure"]
    approved_path.write_text(APPROVED, encoding="utf-8")

    split_state = asyncio.run(split_node({**state, **structure_state}, cfg))

    ch01 = cfg.book_dir / split_state["chapter_sources"]["chapter-1"]
    ch02 = cfg.book_dir / split_state["chapter_sources"]["chapter-2"]
    alignment = json.loads(
        (cfg.work_dir / "chapter_sources" / "_alignment.json").read_text(encoding="utf-8")
    )

    assert "Introductory search material" in ch01.read_text(encoding="utf-8")
    assert "Heuristic search material" in ch02.read_text(encoding="utf-8")
    assert alignment["coverage"]["assigned_ratio"] == 1.0
    assert split_state["chapter_titles"]["chapter-1"] == "Search Foundations"
    assert not stale.exists()


def test_structure_node_cache_key_changes_when_language_changes(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = TestLLMRuntime()
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    source = sources_dir / "textbook.md"
    source.write_text(
        "# Textbook\n\n"
        "<!-- source_ref: textbook-p001 -->\n\n"
        "Introductory search material.\n",
        encoding="utf-8",
    )
    state = {"book_id": cfg.book_id, "sources_md": ["work/sources_md/textbook.md"]}

    first = asyncio.run(structure_node(state, cfg))
    cfg.language = "en-US"
    second = asyncio.run(structure_node(state, cfg))

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
