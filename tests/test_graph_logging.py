from __future__ import annotations

import logging
from typing import Any

import pytest

from bookwiki.scheduler import graph as graph_module
from bookwiki.scheduler.config import default_config
from bookwiki.scheduler.graph import BookGraph


def test_book_graph_logs_node_start_and_done(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")

    def fake_convert(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        return {"sources_md": ["work/sources_md/source.md"], "cache_hit": False}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "convert", fake_convert)
    graph = BookGraph(cfg=cfg, stop_after="convert")

    with caplog.at_level(logging.INFO):
        graph.invoke({"book_id": cfg.book_id})

    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert "node start name=convert book_id=mini" in messages
    assert "node done name=convert book_id=mini cache_hit=False" in messages


def test_force_from_structure_reuses_converted_sources(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "structure"
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    (sources_dir / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (sources_dir / "beta.md").write_text("# Beta\n", encoding="utf-8")

    seen: dict[str, Any] = {}

    def fake_structure(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        seen.update(state)
        return {
            "proposed_structure": "work/structure/proposed-structure.yaml",
            "approved_structure": "work/structure/approved-structure.yaml",
            "cache_hit": False,
        }

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "structure", fake_structure)
    graph = BookGraph(cfg=cfg, stop_after="structure")
    state = graph.invoke({"book_id": cfg.book_id})

    assert seen["sources_md"] == [
        "work/sources_md/alpha.md",
        "work/sources_md/beta.md",
    ]
    assert state["sources_md"] == seen["sources_md"]
