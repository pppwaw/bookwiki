from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from bookwiki.agents.source_summary_agent import SourceSummaryAgent
from bookwiki.agents.structure_agent import StructureAgent, _group_into_two_level
from bookwiki.pipeline.nodes import (
    APPROVED_STRUCTURE_MARKER,
    PENDING_STRUCTURE_MARKER,
    split_node,
    structure_node,
)
from bookwiki.scheduler.config import default_config
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.source import ChapterProposal, StructureResult
from bookwiki.split.chapter_splitter import (
    chapter_groups_from_specs,
    parse_approved_structure,
    split_sources_by_structure,
)

APPROVED = """chapters:
  - title: Search Foundations
    topics:
      - State space search
    source_refs:
      - textbook-p001
  - title: Heuristics
    topics:
      - Heuristic search
    source_refs:
      - textbook-p002
"""

APPROVED_V2 = """chapters:
  - title: Point Estimation
    topics:
      - Method of moments
      - Maximum likelihood estimation
    source_refs:
      - Week-9-p001
      - Week-10-p001
"""


def test_parse_approved_structure_extracts_chapters_and_sources() -> None:
    chapters = parse_approved_structure(APPROVED)

    assert [chapter.chapter_id for chapter in chapters] == ["Search-Foundations", "Heuristics"]
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


def test_parse_approved_structure_accepts_yaml_contract() -> None:
    chapters = parse_approved_structure(APPROVED_V2)

    assert len(chapters) == 1
    assert chapters[0].chapter_id == "Point-Estimation"
    assert chapters[0].title == "Point Estimation"
    assert chapters[0].topics == ["Method of moments", "Maximum likelihood estimation"]
    assert chapters[0].source_refs == ["Week-9-p001", "Week-10-p001"]


def test_parse_approved_structure_slugifies_chapter_n_title_verbatim() -> None:
    # A title that already carries a "Chapter N" prefix is kept verbatim; the id is its slug.
    yaml_text = (
        "chapters:\n"
        "  - title: 'Chapter 6: Point Estimation'\n"
        "    topics: [Method of moments]\n"
        "    source_refs: [Week-9-p001]\n"
    )
    chapters = parse_approved_structure(yaml_text)
    assert chapters[0].chapter_id == "Chapter-6-Point-Estimation"
    assert chapters[0].title == "Chapter 6: Point Estimation"


FREE_FORM = """chapters:
  - title: Search Foundations
    topics:
      - State space search
    source_refs:
      - textbook-p001
  - title: 知识图谱总览
    topics:
      - Concept map
    source_refs:
      - textbook-p002
"""


def test_parse_approved_structure_accepts_free_form_cjk_title() -> None:
    chapters = parse_approved_structure(FREE_FORM)

    # The id is the slug of the title (CJK preserved); the title is kept verbatim.
    assert [chapter.chapter_id for chapter in chapters] == ["Search-Foundations", "知识图谱总览"]
    assert chapters[1].title == "知识图谱总览"
    assert chapters[1].source_refs == ["textbook-p002"]


def test_parse_approved_structure_dedups_duplicate_titles() -> None:
    yaml_text = (
        "chapters:\n"
        "  - title: 复习\n"
        "    topics: [a]\n"
        "    source_refs: [p001]\n"
        "  - title: 复习\n"
        "    topics: [b]\n"
        "    source_refs: [p002]\n"
    )
    chapters = parse_approved_structure(yaml_text)
    # First occurrence keeps the bare slug; the collision gets a deterministic numeric suffix.
    assert [c.chapter_id for c in chapters] == ["复习", "复习-2"]


def test_parse_approved_structure_still_requires_source_refs() -> None:
    empty_refs = FREE_FORM.replace("      - textbook-p002\n", "")

    with pytest.raises(ValueError, match="source_refs"):
        parse_approved_structure(empty_refs)


def test_split_renders_free_form_chapter_with_verbatim_heading(tmp_path: Path) -> None:
    source = tmp_path / "textbook.md"
    source.write_text(
        "# Textbook\n\n"
        "<!-- source_ref: textbook-p001 -->\n\n"
        "States, actions, goals, and search trees.\n\n"
        "<!-- source_ref: textbook-p002 -->\n\n"
        "A bird's-eye concept map of the whole book.\n",
        encoding="utf-8",
    )

    result = split_sources_by_structure([source], FREE_FORM)

    assert "知识图谱总览" in result.chapters
    body = result.chapters["知识图谱总览"]
    # H1 is the human title verbatim — no slug echo and no synthesised "Chapter N" prefix.
    assert body.startswith("# 知识图谱总览")
    assert "textbook-p002" in body
    assert result.chapter_titles["知识图谱总览"] == "知识图谱总览"


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

    assert [chapter["title"] for chapter in structure["chapters"]] == ["Chapter 6 Point Estimation"]
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

    assert "textbook-p001" in result.chapters["Search-Foundations"]
    assert "textbook-p002" not in result.chapters["Search-Foundations"]
    assert "A star search" in result.chapters["Heuristics"]
    assert "textbook-p099" in result.chapters["appendix"]
    assert any(
        item["source_ref"] == "textbook-p001"
        and item["chapter_id"] == "Search-Foundations"
        and item["confidence"] == 1.0
        for item in result.alignment
    )
    assert "| textbook | 1 | 1 | 1 |" in result.report_md


def test_split_sources_by_structure_accepts_page_ref_ranges(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text(
        "# Source\n\n"
        "<!-- source_ref: source-p001 -->\n\n"
        "Intro material.\n\n"
        "<!-- source_ref: source-p002 -->\n\n"
        "Point estimation table.\n\n"
        "<!-- source_ref: source-p003 -->\n\n"
        "Point estimation continuation.\n\n"
        "<!-- source_ref: source-p004 -->\n\n"
        "Appendix material.\n",
        encoding="utf-8",
    )
    approved = """chapters:
  - title: Point Estimation
    topics:
      - Point estimation
    source_refs:
      - source-p002..source-p003
"""

    result = split_sources_by_structure([source], approved)

    assert "source-p002" in result.chapters["Point-Estimation"]
    assert "source-p003" in result.chapters["Point-Estimation"]
    assert "source-p001" not in result.chapters["Point-Estimation"]
    assert "source-p004" in result.chapters["appendix"]


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
    approved_path.write_text(f"{APPROVED_STRUCTURE_MARKER}\n{APPROVED}", encoding="utf-8")

    split_state = asyncio.run(split_node({**state, **structure_state}, cfg))

    ch01 = cfg.book_dir / split_state["chapter_sources"]["Search-Foundations"]
    ch02 = cfg.book_dir / split_state["chapter_sources"]["Heuristics"]
    alignment = json.loads(
        (cfg.work_dir / "chapter_sources" / "_alignment.json").read_text(encoding="utf-8")
    )

    assert "Introductory search material" in ch01.read_text(encoding="utf-8")
    assert "Heuristic search material" in ch02.read_text(encoding="utf-8")
    assert alignment["coverage"]["assigned_ratio"] == 1.0
    assert split_state["chapter_titles"]["Search-Foundations"] == "Search Foundations"
    assert split_state["chapter_topics"]["Search-Foundations"] == ["State space search"]
    assert split_state["chapter_topics"]["Heuristics"] == ["Heuristic search"]
    assert not stale.exists()


NESTED_GROUPS = """chapters:
  - title: Chapter 9 Infinite Series
    sections:
      - title: 9.2 Infinite Series
        topics:
          - infinite series
        source_refs:
          - 9.2-infinite-series-p001
      - title: '9.5 Alternating Series'
        topics:
          - alternating series
        source_refs:
          - 9.5-alternating-series-p001
  - title: Chapter 11 Vectors
    sections:
      - title: 11.5 Vector Functions
        topics:
          - vector functions
        source_refs:
          - 11.5-vector-functions-p001
"""


def test_parse_approved_structure_expands_nested_groups() -> None:
    specs = parse_approved_structure(NESTED_GROUPS)
    assert [spec.chapter_id for spec in specs] == [
        "9.2-Infinite-Series",
        "9.5-Alternating-Series",
        "11.5-Vector-Functions",
    ]
    assert specs[0].group_id == "Chapter-9-Infinite-Series"
    assert specs[0].group_title == "Chapter 9 Infinite Series"
    assert specs[0].title == "9.2 Infinite Series"
    assert specs[2].group_id == "Chapter-11-Vectors"
    groups = chapter_groups_from_specs(specs)
    assert groups["Chapter-9-Infinite-Series"]["leaf_ids"] == [
        "9.2-Infinite-Series",
        "9.5-Alternating-Series",
    ]
    assert groups["Chapter-9-Infinite-Series"]["title"] == "Chapter 9 Infinite Series"
    assert groups["Chapter-11-Vectors"]["leaf_ids"] == ["11.5-Vector-Functions"]


def test_parse_approved_structure_rejects_group_mixed_with_source_refs() -> None:
    bad = (
        "chapters:\n"
        "  - title: Chapter 9 Infinite Series\n"
        "    source_refs: [x-p001]\n"
        "    sections:\n"
        "      - title: 9.2 Infinite Series\n"
        "        topics: [series]\n"
        "        source_refs: [x-p001]\n"
    )
    with pytest.raises(ValueError, match="must not mix"):
        parse_approved_structure(bad)


NON_CHAPTER_GROUP = """chapters:
  - title: 附录合集
    sections:
      - title: 常用分布表
        topics:
          - distribution tables
        source_refs:
          - appendix-p001
      - title: 公式速查
        topics:
          - formula sheet
        source_refs:
          - appendix-p002
"""


def test_parse_approved_structure_accepts_free_form_group() -> None:
    specs = parse_approved_structure(NON_CHAPTER_GROUP)

    # Group and section ids are the slugs of their (verbatim) titles.
    assert [spec.chapter_id for spec in specs] == ["常用分布表", "公式速查"]
    assert specs[0].group_id == "附录合集"
    assert specs[0].group_title == "附录合集"
    assert specs[0].title == "常用分布表"
    groups = chapter_groups_from_specs(specs)
    assert groups["附录合集"]["title"] == "附录合集"
    assert groups["附录合集"]["leaf_ids"] == ["常用分布表", "公式速查"]


def test_split_renders_free_form_group_leaf_with_verbatim_heading(tmp_path: Path) -> None:
    source = tmp_path / "appendix.md"
    source.write_text(
        "<!-- source_ref: appendix-p001 -->\n\nCommon distribution tables.\n\n"
        "<!-- source_ref: appendix-p002 -->\n\nFormula quick reference.\n",
        encoding="utf-8",
    )

    result = split_sources_by_structure([source], NON_CHAPTER_GROUP)

    assert set(result.chapters) >= {"常用分布表", "公式速查"}
    assert "附录合集" not in result.chapters
    body = result.chapters["常用分布表"]
    assert body.startswith("# 常用分布表")
    assert result.chapter_groups["附录合集"]["title"] == "附录合集"
    assert result.chapter_groups["附录合集"]["leaf_ids"] == ["常用分布表", "公式速查"]


def test_split_sources_by_structure_keeps_leaves_flat_with_group_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "calc.md"
    source.write_text(
        "<!-- source_ref: 9.2-infinite-series-p001 -->\n\nSeries material.\n\n"
        "<!-- source_ref: 9.5-alternating-series-p001 -->\n\nAlternating material.\n\n"
        "<!-- source_ref: 11.5-vector-functions-p001 -->\n\nVector material.\n",
        encoding="utf-8",
    )
    result = split_sources_by_structure([source], NESTED_GROUPS)

    # Heavy stages stay flat: chapters keyed by leaf id, not group id.
    assert set(result.chapters) >= {
        "9.2-Infinite-Series",
        "9.5-Alternating-Series",
        "11.5-Vector-Functions",
    }
    assert "Chapter-9-Infinite-Series" not in result.chapters
    assert result.chapter_groups["Chapter-9-Infinite-Series"]["leaf_ids"] == [
        "9.2-Infinite-Series",
        "9.5-Alternating-Series",
    ]
    assert result.chapter_groups["Chapter-11-Vectors"]["leaf_ids"] == ["11.5-Vector-Functions"]


def test_group_into_two_level_nests_sections_and_keeps_chapter_titles_flat() -> None:
    flat = StructureResult(
        chapters=[
            ChapterProposal(
                title="9.2 Infinite Series", topics=["series"], source_refs=["9.2-p001"]
            ),
            ChapterProposal(
                title="9.5 Alternating Series",
                topics=["alternating"],
                source_refs=["9.5-p001"],
            ),
            ChapterProposal(
                title="11.5 Vector Functions",
                topics=["vectors"],
                source_refs=["11.5-p001"],
            ),
            ChapterProposal(
                title="Chapter 6 Point Estimation",
                topics=["mle"],
                source_refs=["w9-p001"],
            ),
        ]
    )

    grouped = _group_into_two_level(flat)

    assert [chapter.title for chapter in grouped.chapters] == [
        "Chapter 9",
        "Chapter 11",
        "Chapter 6 Point Estimation",
    ]
    assert [section.title for section in grouped.chapters[0].sections] == [
        "9.2 Infinite Series",
        "9.5 Alternating Series",
    ]
    # Section-style leaves carry no top-level topics/source_refs; the group wraps them.
    assert grouped.chapters[0].topics == []
    # A real "Chapter N" title stays a flat leaf, not wrapped in a group.
    assert grouped.chapters[2].sections == []
    assert grouped.chapters[2].source_refs == ["w9-p001"]

    # Round-trip: the proposed YAML the agent emits must parse back through the gate parser.
    specs = parse_approved_structure(grouped.proposed_structure_yaml)
    assert [spec.chapter_id for spec in specs] == [
        "9.2-Infinite-Series",
        "9.5-Alternating-Series",
        "11.5-Vector-Functions",
        "Chapter-6-Point-Estimation",
    ]
    assert specs[0].group_id == "Chapter-9"
    assert specs[0].group_title == "Chapter 9"
    assert specs[3].group_id is None


def test_structure_node_reseeds_emptied_approved_structure(tmp_path: Path) -> None:
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

    # User emptied the previously approved file to force a fresh review.
    approved_path = cfg.work_dir / "structure" / "approved-structure.yaml"
    approved_path.parent.mkdir(parents=True, exist_ok=True)
    approved_path.write_text("", encoding="utf-8")

    structure_state = asyncio.run(structure_node(state, cfg))

    seeded = (cfg.book_dir / structure_state["approved_structure"]).read_text(encoding="utf-8")
    assert seeded.strip(), "emptied approved-structure should be re-seeded with pending template"
    assert seeded.splitlines()[0].strip() == PENDING_STRUCTURE_MARKER
    # Not yet approved: no standalone line equals the approval marker.
    assert not any(line.strip() == APPROVED_STRUCTURE_MARKER for line in seeded.splitlines())


def test_split_node_requires_reviewed_approved_structure_marker(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    sources_dir = cfg.work_dir / "sources_md"
    structure_dir = cfg.work_dir / "structure"
    sources_dir.mkdir(parents=True)
    structure_dir.mkdir(parents=True)
    source = sources_dir / "textbook.md"
    source.write_text(
        "<!-- source_ref: textbook-p001 -->\n\nIntroductory search material.",
        encoding="utf-8",
    )
    approved_path = structure_dir / "approved-structure.yaml"
    approved_path.write_text(APPROVED, encoding="utf-8")

    state = {
        "book_id": cfg.book_id,
        "sources_md": ["work/sources_md/textbook.md"],
        "approved_structure": "work/structure/approved-structure.yaml",
    }

    try:
        asyncio.run(split_node(state, cfg))
    except ValueError as exc:
        assert APPROVED_STRUCTURE_MARKER in str(exc)
    else:
        raise AssertionError("split_node should require an approved structure marker")


def test_structure_node_cache_key_changes_when_language_changes(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = TestLLMRuntime()
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    source = sources_dir / "textbook.md"
    source.write_text(
        "# Textbook\n\n<!-- source_ref: textbook-p001 -->\n\nIntroductory search material.\n",
        encoding="utf-8",
    )
    state = {"book_id": cfg.book_id, "sources_md": ["work/sources_md/textbook.md"]}

    first = asyncio.run(structure_node(state, cfg))
    cfg.language = "en-US"
    second = asyncio.run(structure_node(state, cfg))

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False


def test_structure_node_cache_key_changes_when_book_notes_change(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = TestLLMRuntime()
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    source = sources_dir / "textbook.md"
    source.write_text(
        "# Textbook\n\n<!-- source_ref: textbook-p001 -->\n\nIntroductory search material.\n",
        encoding="utf-8",
    )
    cfg.notes_file.write_text(
        "Use English concept names alongside Chinese translations.",
        encoding="utf-8",
    )
    state = {"book_id": cfg.book_id, "sources_md": ["work/sources_md/textbook.md"]}

    first = asyncio.run(structure_node(state, cfg))
    cfg.notes_file.write_text(
        "Use Chinese concept names only; keep English terms in parentheses.",
        encoding="utf-8",
    )
    second = asyncio.run(structure_node(state, cfg))

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
