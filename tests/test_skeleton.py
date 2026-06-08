"""Unit tests for the M2 build-skeleton stage.

Covers four pieces:

* ``SkeletonAgent``: deterministic draft (topics → canonical glossary,
  first-occurrence ownership, alias map, chapter briefs).
* ``build_skeleton_node``: end-to-end run via ``TestLLMRuntime``, ensuring the
  skeleton lands on disk and exposes the expected state key.
* ``_skeleton_payload``: per-chapter projection (prev/next briefs, chapter-owns
  vs chapter-uses split).
* ``_merge_candidates_with_skeleton``: slim reconcile path that re-uses the
  skeleton's pre-merged glossary and only adds genuinely new candidates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bookwiki.agents.skeleton_agent import SkeletonAgent
from bookwiki.pipeline.nodes import (
    _merge_candidates_with_skeleton,
    _skeleton_payload,
    build_skeleton_node,
)
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.skeleton import BookSkeleton


# --------------------------------------------------------------------------- #
# SkeletonAgent — deterministic draft (TestLLMRuntime echoes the draft back)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_skeleton_agent_assigns_first_chapter_by_order() -> None:
    """Duplicate topic across chapters → first_chapter_id is the earliest."""
    payload: dict[str, Any] = {
        "chapters": [
            {"chapter_id": "chapter-1", "title": "Intro", "topics": ["Bayes"], "source_md": ""},
            {"chapter_id": "chapter-2", "title": "Deep", "topics": ["Bayes"], "source_md": ""},
        ],
        "language": "zh-CN",
        "book_notes": "",
    }

    result = await SkeletonAgent().run(payload, model="stub", runtime=TestLLMRuntime())

    assert isinstance(result, BookSkeleton)
    canonicals = [c.canonical for c in result.glossary]
    assert canonicals == ["Bayes"]  # single canonical, not duplicated
    assert result.glossary[0].first_chapter_id == "chapter-1"


@pytest.mark.asyncio
async def test_skeleton_agent_alias_map_includes_raw_and_normalised() -> None:
    """alias_map exposes both the raw canonical and its normalised key."""
    payload = {
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "title": "T",
                "topics": ["Method of Moments"],
                "source_md": "",
            },
        ],
        "language": "zh-CN",
        "book_notes": "",
    }

    result = await SkeletonAgent().run(payload, model="stub", runtime=TestLLMRuntime())

    assert result.alias_map["Method of Moments"] == "Method of Moments"
    assert result.alias_map["methodofmoments"] == "Method of Moments"


@pytest.mark.asyncio
async def test_skeleton_agent_emits_brief_and_order_for_each_chapter() -> None:
    payload = {
        "chapters": [
            {"chapter_id": "chapter-1", "title": "A", "topics": ["t1", "t2"], "source_md": ""},
            {"chapter_id": "chapter-2", "title": "B", "topics": ["t3"], "source_md": ""},
        ],
        "language": "zh-CN",
        "book_notes": "",
    }

    result = await SkeletonAgent().run(payload, model="stub", runtime=TestLLMRuntime())

    assert set(result.chapter_briefs) == {"chapter-1", "chapter-2"}
    assert all(brief for brief in result.chapter_briefs.values())  # non-empty
    assert result.chapter_order == ["chapter-1", "chapter-2"]


# --------------------------------------------------------------------------- #
# build_skeleton_node — end-to-end against a mini on-disk fixture
# --------------------------------------------------------------------------- #
def _write_chapter_source(book_dir: Path, chapter_id: str, body: str) -> str:
    path = book_dir / "work" / "chapter_sources" / chapter_id / "source.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return f"work/chapter_sources/{chapter_id}/source.md"


@pytest.mark.asyncio
async def test_build_skeleton_node_writes_skeleton_json(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    rel_a = _write_chapter_source(book_dir, "chapter-1", "# Intro\n<!-- source_ref: src-p001 -->\n")
    rel_b = _write_chapter_source(book_dir, "chapter-2", "# Deep\n<!-- source_ref: src-p002 -->\n")
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "chapter_sources": {"chapter-1": rel_a, "chapter-2": rel_b},
        "chapter_titles": {"chapter-1": "Intro", "chapter-2": "Deep"},
        "chapter_topics": {"chapter-1": ["Bayes"], "chapter-2": ["MLE"]},
    }

    delta = await build_skeleton_node(state, cfg)

    skeleton_path = book_dir / delta["skeleton"]
    assert skeleton_path.exists()
    assert delta["cache_hit"] is False
    # rerun should hit the cache
    delta2 = await build_skeleton_node(state, cfg)
    assert delta2["cache_hit"] is True


# --------------------------------------------------------------------------- #
# _skeleton_payload — per-chapter projection
# --------------------------------------------------------------------------- #
def _sample_skeleton() -> dict[str, Any]:
    return {
        "glossary": [
            {"canonical": "Bayes", "aliases": ["Bayes Rule"], "first_chapter_id": "chapter-1"},
            {"canonical": "MLE", "aliases": [], "first_chapter_id": "chapter-2"},
            {"canonical": "MAP", "aliases": [], "first_chapter_id": "chapter-3"},
        ],
        "alias_map": {
            "Bayes": "Bayes",
            "Bayes Rule": "Bayes",
            "bayes": "Bayes",
            "MLE": "MLE",
            "mle": "MLE",
            "MAP": "MAP",
            "map": "MAP",
        },
        "chapter_briefs": {
            "chapter-1": "Intro: Bayes",
            "chapter-2": "Deep: MLE",
            "chapter-3": "Advanced: MAP",
        },
        "chapter_order": ["chapter-1", "chapter-2", "chapter-3"],
    }


def test_skeleton_payload_returns_empty_when_skeleton_missing() -> None:
    assert _skeleton_payload(None, "chapter-1") == {}


def test_skeleton_payload_splits_owns_vs_uses_and_picks_neighbours() -> None:
    skeleton = _sample_skeleton()

    payload = _skeleton_payload(skeleton, "chapter-2")

    owns_canonicals = [item["canonical"] for item in payload["chapter_owns"]]
    uses_canonicals = [item["canonical"] for item in payload["chapter_uses"]]
    assert owns_canonicals == ["MLE"]
    assert set(uses_canonicals) == {"Bayes", "MAP"}
    assert payload["prev_brief"] == "Intro: Bayes"
    assert payload["next_brief"] == "Advanced: MAP"
    assert payload["alias_map"]["Bayes Rule"] == "Bayes"
    assert payload["glossary"] == skeleton["glossary"]


def test_skeleton_payload_first_and_last_chapter_have_one_sided_briefs() -> None:
    skeleton = _sample_skeleton()

    first = _skeleton_payload(skeleton, "chapter-1")
    last = _skeleton_payload(skeleton, "chapter-3")

    assert first["prev_brief"] == ""
    assert first["next_brief"] == "Deep: MLE"
    assert last["prev_brief"] == "Deep: MLE"
    assert last["next_brief"] == ""


# --------------------------------------------------------------------------- #
# _merge_candidates_with_skeleton — slim reconcile path
# --------------------------------------------------------------------------- #
def test_merge_preserves_skeleton_glossary_when_no_new_candidates() -> None:
    skeleton = _sample_skeleton()

    result = _merge_candidates_with_skeleton(skeleton, candidates=[])

    canonicals = [c.canonical for c in result.concepts]
    assert canonicals == ["Bayes", "MLE", "MAP"]
    # alias map preserved + canonical self-reference present
    assert result.alias_map["Bayes Rule"] == "Bayes"
    assert result.alias_map["Bayes"] == "Bayes"


def test_merge_attaches_candidate_chapter_to_existing_canonical() -> None:
    skeleton = _sample_skeleton()
    # ConceptExtractAgent in chapter-2 mentioned "Bayes Rule" → should attach to "Bayes"
    candidates = [
        {
            "name": "Bayes Rule",
            "aliases": [],
            "source_chapter_id": "chapter-2",
            "owner_task_id": "chapter-2:concept_extract",
        }
    ]

    result = _merge_candidates_with_skeleton(skeleton, candidates)

    bayes = next(c for c in result.concepts if c.canonical == "Bayes")
    assert "chapter-2" in bayes.source_chapter_ids


def test_merge_adds_new_concept_not_present_in_skeleton() -> None:
    skeleton = _sample_skeleton()
    candidates = [
        {
            "name": "Cramer-Rao Bound",
            "aliases": ["CRB"],
            "source_chapter_id": "chapter-3",
            "owner_task_id": "chapter-3:concept_extract",
        }
    ]

    result = _merge_candidates_with_skeleton(skeleton, candidates)

    canonicals = [c.canonical for c in result.concepts]
    assert "Cramer-Rao Bound" in canonicals
    assert result.alias_map["Cramer-Rao Bound"] == "Cramer-Rao Bound"
    assert result.alias_map["CRB"] == "Cramer-Rao Bound"


def test_merge_groups_variants_of_the_same_new_concept() -> None:
    skeleton = _sample_skeleton()
    candidates = [
        {
            "name": "Fisher Information",
            "aliases": [],
            "source_chapter_id": "chapter-2",
            "owner_task_id": "x",
        },
        {
            "name": "fisher information",
            "aliases": [],
            "source_chapter_id": "chapter-3",
            "owner_task_id": "y",
        },
    ]

    result = _merge_candidates_with_skeleton(skeleton, candidates)

    fishers = [c for c in result.concepts if c.canonical.lower().startswith("fisher")]
    assert len(fishers) == 1, "variants must collapse into one canonical entry"
    assert {"chapter-2", "chapter-3"}.issubset(fishers[0].source_chapter_ids)
