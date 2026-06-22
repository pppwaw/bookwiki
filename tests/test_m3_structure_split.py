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


NON_ORIGINAL = """chapters:
  - title: Chapter 1 Search Foundations
    topics:
      - State space search
    source_refs:
      - textbook-p001
  - id: knowledge-overview
    title: 知识图谱总览
    topics:
      - Concept map
    source_refs:
      - textbook-p002
"""


def test_parse_approved_structure_accepts_non_chapter_title_with_explicit_id() -> None:
    chapters = parse_approved_structure(NON_ORIGINAL)

    assert [chapter.chapter_id for chapter in chapters] == ["chapter-1", "knowledge-overview"]
    # The non-original chapter keeps its free-form title verbatim (no "Chapter N" prefix stripped).
    assert chapters[1].title == "知识图谱总览"
    assert chapters[1].source_refs == ["textbook-p002"]


def test_parse_approved_structure_rejects_non_chapter_title_without_id() -> None:
    missing_id = NON_ORIGINAL.replace(
        "  - id: knowledge-overview\n    title: 知识图谱总览\n",
        "  - title: 知识图谱总览\n",
    )

    with pytest.raises(ValueError, match="chapter titles"):
        parse_approved_structure(missing_id)


def test_parse_approved_structure_rejects_reserved_explicit_id() -> None:
    reserved = NON_ORIGINAL.replace("id: knowledge-overview", "id: chapter-3")

    with pytest.raises(ValueError, match="reserved"):
        parse_approved_structure(reserved)


def test_parse_approved_structure_rejects_non_ascii_explicit_id() -> None:
    non_ascii = NON_ORIGINAL.replace("id: knowledge-overview", "id: 知识图谱")

    with pytest.raises(ValueError, match="ASCII slug"):
        parse_approved_structure(non_ascii)


def test_parse_approved_structure_non_original_still_requires_source_refs() -> None:
    empty_refs = NON_ORIGINAL.replace("      - textbook-p002\n", "")

    with pytest.raises(ValueError, match="source_refs"):
        parse_approved_structure(empty_refs)


def test_split_renders_non_original_chapter_with_verbatim_heading(tmp_path: Path) -> None:
    source = tmp_path / "textbook.md"
    source.write_text(
        "# Textbook\n\n"
        "<!-- source_ref: textbook-p001 -->\n\n"
        "States, actions, goals, and search trees.\n\n"
        "<!-- source_ref: textbook-p002 -->\n\n"
        "A bird's-eye concept map of the whole book.\n",
        encoding="utf-8",
    )

    result = split_sources_by_structure([source], NON_ORIGINAL)

    assert "knowledge-overview" in result.chapters
    body = result.chapters["knowledge-overview"]
    # H1 is the human title verbatim — neither the slug id nor a "Chapter N" prefix is injected.
    assert body.startswith("# 知识图谱总览")
    assert "knowledge-overview 知识图谱总览" not in body
    assert "textbook-p002" in body
    assert result.chapter_titles["knowledge-overview"] == "知识图谱总览"


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
  - title: Chapter 6 Point Estimation
    topics:
      - Point estimation
    source_refs:
      - source-p002..source-p003
"""

    result = split_sources_by_structure([source], approved)

    assert "source-p002" in result.chapters["chapter-6"]
    assert "source-p003" in result.chapters["chapter-6"]
    assert "source-p001" not in result.chapters["chapter-6"]
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

    ch01 = cfg.book_dir / split_state["chapter_sources"]["chapter-1"]
    ch02 = cfg.book_dir / split_state["chapter_sources"]["chapter-2"]
    alignment = json.loads(
        (cfg.work_dir / "chapter_sources" / "_alignment.json").read_text(encoding="utf-8")
    )

    assert "Introductory search material" in ch01.read_text(encoding="utf-8")
    assert "Heuristic search material" in ch02.read_text(encoding="utf-8")
    assert alignment["coverage"]["assigned_ratio"] == 1.0
    assert split_state["chapter_titles"]["chapter-1"] == "Search Foundations"
    assert split_state["chapter_topics"]["chapter-1"] == ["State space search"]
    assert split_state["chapter_topics"]["chapter-2"] == ["Heuristic search"]
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
        "chapter-9-2",
        "chapter-9-5",
        "chapter-11-5",
    ]
    assert specs[0].group_id == "chapter-9"
    assert specs[0].group_title == "Chapter 9 Infinite Series"
    assert specs[0].title == "9.2 Infinite Series"
    assert specs[2].group_id == "chapter-11"
    groups = chapter_groups_from_specs(specs)
    assert groups["chapter-9"]["leaf_ids"] == ["chapter-9-2", "chapter-9-5"]
    assert groups["chapter-9"]["title"] == "Chapter 9 Infinite Series"
    assert groups["chapter-11"]["leaf_ids"] == ["chapter-11-5"]


def test_parse_approved_structure_rejects_section_outside_group() -> None:
    bad = (
        "chapters:\n"
        "  - title: Chapter 9 Infinite Series\n"
        "    sections:\n"
        "      - title: 11.5 Wrong Chapter\n"
        "        topics: [vectors]\n"
        "        source_refs: [x-p001]\n"
    )
    with pytest.raises(ValueError, match="does not belong to group"):
        parse_approved_structure(bad)


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


NON_ORIGINAL_GROUP = """chapters:
  - id: appendix-pack
    title: 附录合集
    sections:
      - id: appendix-tables
        title: 常用分布表
        topics:
          - distribution tables
        source_refs:
          - appendix-p001
      - id: appendix-formulas
        title: 公式速查
        topics:
          - formula sheet
        source_refs:
          - appendix-p002
"""


def test_parse_approved_structure_accepts_non_chapter_group_with_explicit_ids() -> None:
    specs = parse_approved_structure(NON_ORIGINAL_GROUP)

    assert [spec.chapter_id for spec in specs] == ["appendix-tables", "appendix-formulas"]
    # Group and section titles are kept verbatim (no "Chapter N" prefix injected).
    assert specs[0].group_id == "appendix-pack"
    assert specs[0].group_title == "附录合集"
    assert specs[0].title == "常用分布表"
    groups = chapter_groups_from_specs(specs)
    assert groups["appendix-pack"]["title"] == "附录合集"
    assert groups["appendix-pack"]["leaf_ids"] == ["appendix-tables", "appendix-formulas"]


def test_parse_approved_structure_rejects_non_chapter_group_without_id() -> None:
    missing_group_id = NON_ORIGINAL_GROUP.replace(
        "  - id: appendix-pack\n    title: 附录合集\n",
        "  - title: 附录合集\n",
    )

    with pytest.raises(ValueError, match="explicit ASCII 'id'"):
        parse_approved_structure(missing_group_id)


def test_parse_approved_structure_rejects_non_numbered_section_without_id() -> None:
    missing_section_id = NON_ORIGINAL_GROUP.replace(
        "      - id: appendix-tables\n        title: 常用分布表\n",
        "      - title: 常用分布表\n",
    )

    with pytest.raises(ValueError, match="explicit ASCII 'id'"):
        parse_approved_structure(missing_section_id)


def test_parse_approved_structure_allows_numbered_section_under_non_chapter_group() -> None:
    mixed = """chapters:
  - id: review-pack
    title: 综合复习
    sections:
      - title: 9.2 Infinite Series
        topics:
          - series
        source_refs:
          - review-p001
"""

    specs = parse_approved_structure(mixed)

    # Numbered section keeps its derived id; parent-number check is skipped for non-chapter groups.
    assert specs[0].chapter_id == "chapter-9-2"
    assert specs[0].group_id == "review-pack"
    assert specs[0].title == "9.2 Infinite Series"


def test_split_renders_non_chapter_group_leaf_with_verbatim_heading(tmp_path: Path) -> None:
    source = tmp_path / "appendix.md"
    source.write_text(
        "<!-- source_ref: appendix-p001 -->\n\nCommon distribution tables.\n\n"
        "<!-- source_ref: appendix-p002 -->\n\nFormula quick reference.\n",
        encoding="utf-8",
    )

    result = split_sources_by_structure([source], NON_ORIGINAL_GROUP)

    assert set(result.chapters) >= {"appendix-tables", "appendix-formulas"}
    assert "appendix-pack" not in result.chapters
    body = result.chapters["appendix-tables"]
    assert body.startswith("# 常用分布表")
    assert "appendix-tables 常用分布表" not in body
    assert result.chapter_groups["appendix-pack"]["title"] == "附录合集"
    assert result.chapter_groups["appendix-pack"]["leaf_ids"] == [
        "appendix-tables",
        "appendix-formulas",
    ]


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
    assert set(result.chapters) >= {"chapter-9-2", "chapter-9-5", "chapter-11-5"}
    assert "chapter-9" not in result.chapters
    assert result.chapter_groups["chapter-9"]["leaf_ids"] == ["chapter-9-2", "chapter-9-5"]
    assert result.chapter_groups["chapter-11"]["leaf_ids"] == ["chapter-11-5"]


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
        "chapter-9-2",
        "chapter-9-5",
        "chapter-11-5",
        "chapter-6",
    ]
    assert specs[0].group_id == "chapter-9"
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
