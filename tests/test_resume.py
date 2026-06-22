"""Unit tests for the pure resume / force-from helpers in `scheduler.resume`.

These port the state-reconstruction coverage that previously lived in
`test_graph_logging` (against the legacy `BookGraph`) to direct tests of the
extracted functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bookwiki.scheduler.config import default_config
from bookwiki.scheduler.resume import (
    NODE_ORDER,
    config_hash,
    draw_mermaid,
    existing_split_state,
    state_after_config_change,
    state_for_force_from,
)


def test_config_hash_changes_with_language(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    before = config_hash(cfg)
    cfg.language = "en-US"
    assert config_hash(cfg) != before


def test_draw_mermaid_exposes_full_topology() -> None:
    mermaid = draw_mermaid()
    assert mermaid.startswith("graph TD")
    assert "START --> convert" in mermaid
    assert "convert --> caption" in mermaid
    assert "check -->|issues| repair" in mermaid
    assert "check -->|clean| index" in mermaid
    assert "repair --> integrate" in mermaid
    assert "index --> END" in mermaid
    for node in NODE_ORDER:
        assert node in mermaid


def test_force_from_structure_reuses_converted_sources(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "structure"
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    (sources_dir / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (sources_dir / "beta.md").write_text("# Beta\n", encoding="utf-8")

    state = state_for_force_from(cfg, {})

    assert state["sources_md"] == ["work/sources_md/alpha.md", "work/sources_md/beta.md"]


def test_force_from_caption_reuses_sources_and_manifests(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "caption"
    (cfg.work_dir / "sources_md").mkdir(parents=True)
    (cfg.work_dir / "source_refs").mkdir(parents=True)
    (cfg.work_dir / "sources_md" / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (cfg.work_dir / "source_refs" / "alpha.json").write_text(
        '{"source_id":"alpha","pages":[]}', encoding="utf-8"
    )

    state = state_for_force_from(cfg, {})

    assert state["sources_md"] == ["work/sources_md/alpha.md"]
    assert state["source_ref_manifests"] == ["work/source_refs/alpha.json"]


def test_force_from_generate_drops_downstream_and_keeps_split(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "generate"
    checkpoint = {
        "book_id": cfg.book_id,
        "sources_md": ["work/sources_md/source.md"],
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "chapter_titles": {"chapter-1": "Intro"},
        "agent_results": {"stale": {}},
    }

    state = state_for_force_from(cfg, checkpoint)

    assert state["chapter_sources"] == {"chapter-1": "work/chapter_sources/chapter-1/source.md"}
    assert state["chapter_titles"] == {"chapter-1": "Intro"}
    assert "agent_results" not in state


def test_force_from_generate_reconstructs_split_state_from_files(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "generate"
    source_path = cfg.work_dir / "chapter_sources" / "chapter-1" / "source.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# Intro\n", encoding="utf-8")
    (cfg.work_dir / "chapter_sources" / "_alignment.json").write_text(
        '{"chapter_titles": {"chapter-1": "Intro"}}', encoding="utf-8"
    )

    state = state_for_force_from(cfg, {"sources_md": ["work/sources_md/source.md"]})

    assert state["chapter_sources"] == {"chapter-1": "work/chapter_sources/chapter-1/source.md"}
    assert state["chapter_titles"] == {"chapter-1": "Intro"}


def test_state_after_config_change_salvages_from_caption(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    checkpoint: dict[str, Any] = {
        "book_id": cfg.book_id,
        "sources_md": ["work/sources_md/a.md"],
        "source_ref_manifests": ["work/source_refs/a.json"],
    }

    state, start_index = state_after_config_change(cfg, checkpoint, stop_after=None)

    assert start_index == NODE_ORDER.index("caption")
    assert state["sources_md"] == ["work/sources_md/a.md"]
    assert state["source_ref_manifests"] == ["work/source_refs/a.json"]


def test_state_after_config_change_falls_back_to_convert(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")

    state, start_index = state_after_config_change(cfg, {}, stop_after=None)

    assert start_index == 0
    assert state == {"book_id": cfg.book_id}


def _write_split_on_disk(
    cfg: Any, chapter_ids: list[str], *, alignment: dict[str, Any]
) -> None:
    """Write chapter source dirs (created in a deliberately non-YAML filesystem order) plus the
    given ``_alignment.json`` payload, so resume reconstruction can be exercised in isolation."""
    import json

    base = cfg.work_dir / "chapter_sources"
    # Create dirs sorted lexicographically (the order a glob would yield), to prove reconstruction
    # does NOT depend on directory iteration order.
    for ch_id in sorted(chapter_ids):
        source_path = base / ch_id / "source.md"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"# {ch_id}\n", encoding="utf-8")
    (base / "_alignment.json").write_text(json.dumps(alignment), encoding="utf-8")


def test_existing_split_state_preserves_chapter_order_over_glob(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    # Double-digit chapter: lexicographic glob would put "chapter-10" before "chapter-2".
    _write_split_on_disk(
        cfg,
        ["chapter-2", "chapter-10"],
        alignment={
            "chapter_titles": {"chapter-2": "Two", "chapter-10": "Ten"},
            "chapter_order": ["chapter-2", "chapter-10"],
        },
    )

    chapter_sources, _titles, _groups, _align, chapter_order = existing_split_state(cfg)

    assert list(chapter_sources.keys()) == ["chapter-2", "chapter-10"]
    assert chapter_order == ["chapter-2", "chapter-10"]


def test_existing_split_state_legacy_alignment_uses_title_order(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    # Legacy _alignment.json written before chapter_order existed: fall back to chapter_titles
    # key order (also YAML order), never lexicographic glob order.
    _write_split_on_disk(
        cfg,
        ["chapter-2", "chapter-10"],
        alignment={"chapter_titles": {"chapter-2": "Two", "chapter-10": "Ten"}},
    )

    chapter_sources, _titles, _groups, _align, _order = existing_split_state(cfg)

    assert list(chapter_sources.keys()) == ["chapter-2", "chapter-10"]


def test_existing_split_state_fails_loud_on_extra_dir(tmp_path: Path) -> None:
    import pytest

    cfg = default_config(tmp_path / "books" / "mini")
    _write_split_on_disk(
        cfg,
        ["chapter-1", "chapter-stale"],
        alignment={
            "chapter_titles": {"chapter-1": "One"},
            "chapter_order": ["chapter-1"],
        },
    )

    with pytest.raises(ValueError, match="stale split state"):
        existing_split_state(cfg)
