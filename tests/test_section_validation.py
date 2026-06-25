"""Unit tests for the deterministic section-level validation (Phase 3).

``validate_section`` is the section-granularity counterpart of ``check_node``:
it flags unresolvable citations, sections that redefine a concept owned by
another chapter, and concepts introduced under a non-canonical alias.
"""

from __future__ import annotations

from typing import Any

from bookwiki.generate.sections import validate_section
from bookwiki.schemas.common import Citation
from bookwiki.schemas.section import SectionResult, SectionSpec


def _spec(chapter_id: str = "chapter-2", index: int = 0) -> SectionSpec:
    return SectionSpec(
        chapter_id=chapter_id,
        index=index,
        title="Section",
        topics_covered=[],
        concepts_introduced=[],
        learning_goal="goal",
    )


def _section(
    *,
    chapter_id: str = "chapter-2",
    index: int = 0,
    concepts: list[str] | None = None,
    citations: list[Citation] | None = None,
) -> SectionResult:
    return SectionResult(
        chapter_id=chapter_id,
        section_index=index,
        title="Section",
        body_md="Body.",
        concepts=concepts or [],
        citations=citations or [Citation(ref_id="src-p001", quote="quote")],
        figure_requests=[],
        owner_task_id=f"{chapter_id}:section:{index:03d}",
    )


def _skeleton(chapter_uses: list[dict[str, Any]], alias_map: dict[str, str]) -> dict[str, Any]:
    return {"chapter_uses": chapter_uses, "alias_map_slice": alias_map, "chapter_owns": []}


def test_validate_section_passes_clean_section() -> None:
    result = validate_section(
        section=_section(concepts=["MLE"]),
        section_spec=_spec(),
        allowed_refs={"src-p001"},
        skeleton_payload=_skeleton([], {}),
    )

    assert result.ok is True
    assert result.messages == []


def test_validate_section_flags_unknown_citation() -> None:
    result = validate_section(
        section=_section(citations=[Citation(ref_id="bad-ref", quote="quote")]),
        section_spec=_spec(),
        allowed_refs={"src-p001"},
        skeleton_payload=_skeleton([], {}),
    )

    assert result.ok is False
    assert any("unknown source_ref" in message for message in result.messages)
    assert any("bad-ref" in message for message in result.messages)


def test_validate_section_flags_redefining_other_chapter_concept() -> None:
    skeleton = _skeleton(
        chapter_uses=[{"canonical": "Bayes", "aliases": [], "first_chapter_id": "chapter-1"}],
        alias_map={},
    )

    result = validate_section(
        section=_section(concepts=["Bayes"]),
        section_spec=_spec(),
        allowed_refs={"src-p001"},
        skeleton_payload=skeleton,
    )

    assert result.ok is False
    assert any("owned by another chapter" in message for message in result.messages)


def test_validate_section_flags_non_canonical_alias() -> None:
    skeleton = _skeleton(
        chapter_uses=[],
        alias_map={"Bayes Rule": "Bayes", "bayesrule": "Bayes"},
    )

    result = validate_section(
        section=_section(concepts=["Bayes Rule"]),
        section_spec=_spec(),
        allowed_refs={"src-p001"},
        skeleton_payload=skeleton,
    )

    assert result.ok is False
    assert any("non-canonical term" in message for message in result.messages)


def test_validate_section_allows_owned_canonical_concept() -> None:
    skeleton = _skeleton(
        chapter_uses=[],
        alias_map={"Bayes": "Bayes", "bayes": "Bayes"},
    )

    result = validate_section(
        section=_section(concepts=["Bayes"]),
        section_spec=_spec(),
        allowed_refs={"src-p001"},
        skeleton_payload=skeleton,
    )

    assert result.ok is True
