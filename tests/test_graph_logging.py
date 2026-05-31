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


def test_force_from_caption_reuses_converted_sources_and_manifests(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "caption"
    sources_dir = cfg.work_dir / "sources_md"
    refs_dir = cfg.work_dir / "source_refs"
    sources_dir.mkdir(parents=True)
    refs_dir.mkdir(parents=True)
    (sources_dir / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (refs_dir / "alpha.json").write_text('{"source_id":"alpha","pages":[]}', encoding="utf-8")

    seen: dict[str, Any] = {}

    def fake_caption(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        seen.update(state)
        return {"caption_results": [], "cache_hit": True}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "caption", fake_caption)
    graph = BookGraph(cfg=cfg, stop_after="caption")
    state = graph.invoke({"book_id": cfg.book_id})

    assert seen["sources_md"] == ["work/sources_md/alpha.md"]
    assert seen["source_ref_manifests"] == ["work/source_refs/alpha.json"]
    assert "caption_results" not in seen
    assert state["caption_results"] == []


def test_force_from_structure_pauses_before_split_for_manual_review(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.force_from = "structure"
    sources_dir = cfg.work_dir / "sources_md"
    sources_dir.mkdir(parents=True)
    (sources_dir / "source.md").write_text("# Source\n", encoding="utf-8")

    def fake_structure(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        return {
            "proposed_structure": "work/structure/proposed-structure.yaml",
            "approved_structure": "work/structure/approved-structure.yaml",
            "cache_hit": False,
        }

    def fail_split(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        raise AssertionError("split should wait for manual structure approval")

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "structure", fake_structure)
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "split", fail_split)

    state = BookGraph(cfg=cfg).invoke({"book_id": cfg.book_id})

    assert state["approved_structure"] == "work/structure/approved-structure.yaml"
    checkpoint = graph_module.read_json(BookGraph(cfg=cfg).checkpoint_path)
    assert checkpoint["status"] == "paused"
    assert checkpoint["next_node"] == "split"


def test_force_from_generate_reuses_split_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    graph = BookGraph(cfg=cfg)
    graph._write_checkpoint(
        {
            "book_id": cfg.book_id,
            "sources_md": ["work/sources_md/source.md"],
            "approved_structure": "work/structure/approved-structure.yaml",
            "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
            "chapter_titles": {"chapter-1": "Intro"},
            "agent_results": {"stale": {}},
        },
        [],
        status="completed",
        next_index=None,
    )
    cfg.force_from = "generate"
    seen: dict[str, Any] = {}

    def fake_generate(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        seen.update(state)
        return {"agent_results": {"chapter-1": {"chapter": "fresh.json"}}}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "generate", fake_generate)

    state = BookGraph(cfg=cfg, stop_after="generate").invoke({"book_id": cfg.book_id})

    assert seen["chapter_sources"] == {
        "chapter-1": "work/chapter_sources/chapter-1/source.md"
    }
    assert seen["chapter_titles"] == {"chapter-1": "Intro"}
    assert "agent_results" not in seen
    assert state["agent_results"] == {"chapter-1": {"chapter": "fresh.json"}}


def test_force_from_generate_reconstructs_split_state_from_files(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    source_path = cfg.work_dir / "chapter_sources" / "chapter-1" / "source.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# Intro\n", encoding="utf-8")
    alignment_path = cfg.work_dir / "chapter_sources" / "_alignment.json"
    alignment_path.write_text(
        '{"chapter_titles": {"chapter-1": "Intro"}}',
        encoding="utf-8",
    )
    graph = BookGraph(cfg=cfg)
    graph._write_checkpoint(
        {"book_id": cfg.book_id, "sources_md": ["work/sources_md/source.md"]},
        [],
        status="paused",
        next_index=4,
    )
    cfg.force_from = "generate"
    seen: dict[str, Any] = {}

    def fake_generate(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        seen.update(state)
        return {"agent_results": {"chapter-1": {"chapter": "fresh.json"}}}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "generate", fake_generate)

    BookGraph(cfg=cfg, stop_after="generate").invoke({"book_id": cfg.book_id})

    assert seen["chapter_sources"] == {
        "chapter-1": "work/chapter_sources/chapter-1/source.md"
    }
    assert seen["chapter_titles"] == {"chapter-1": "Intro"}


def test_repair_resume_reintegrates_before_honoring_stop_after_check(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    graph = BookGraph(cfg=cfg)
    graph._write_checkpoint(
        {"book_id": cfg.book_id, "repair_targets": ["chapter-1:quiz"]},
        [],
        status="paused",
        next_index=9,
    )
    calls: list[str] = []

    def fake_repair(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        calls.append("repair")
        return {"repairs": ["work/repairs/chapter-1-quiz.json"], "repair_targets": []}

    def fake_integrate(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        calls.append("integrate")
        return {"content_ready": True}

    def fake_check(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        calls.append("check")
        return {"check_report": "work/logs/check-report.json", "repair_targets": []}

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "repair", fake_repair)
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "integrate", fake_integrate)
    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "check", fake_check)

    state = BookGraph(cfg=cfg, stop_after="check").invoke(resume=True)

    assert calls == ["repair", "integrate", "check"]
    assert state["content_ready"] is True
    assert state["repair_targets"] == []


def test_resume_does_not_run_nodes_after_stop_after_target(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    graph = BookGraph(cfg=cfg)
    graph._write_checkpoint(
        {"book_id": cfg.book_id, "check_report": "work/logs/check-report.json"},
        [],
        status="paused",
        next_index=10,
    )

    def fail_index(state: dict[str, Any], cfg_arg) -> dict[str, Any]:  # noqa: ANN001
        raise AssertionError("index must not run when --to check is already behind us")

    monkeypatch.setitem(graph_module.NODE_FUNCTIONS, "index", fail_index)

    state = BookGraph(cfg=cfg, stop_after="check").invoke(resume=True)

    assert state["check_report"] == "work/logs/check-report.json"
