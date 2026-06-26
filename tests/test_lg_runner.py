"""Unit tests for the LangGraph runner's control surface.

These exercise ``run_pipeline`` with monkeypatched fake nodes (fast, no real
LLM) to lock in the resume / stop / force-from / interrupt behaviour that the
slower subprocess CLI tests cover only end-to-end. They mirror the legacy
``test_graph_logging`` coverage but target the LangGraph engine, so the eventual
removal of ``BookGraph`` does not leave the control logic untested.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bookwiki.pipeline import nodes as nodes_module
from bookwiki.scheduler.config import default_config
from bookwiki.scheduler.lg_runner import run_pipeline
from bookwiki.scheduler.llm import record_stage_usage
from bookwiki.utils import logging as logging_utils


def _spend(runtime: Any, *, cost: float, prompt: int, completion: int) -> None:
    """Simulate one recorded API call exactly like ``LiteLLMRuntime._record_usage``:

    bump the shared global counters (budget / interrupted-stage fallback) and
    attribute the same usage to the stage active in the current context.
    """
    runtime.total_cost_cny += cost
    runtime.total_prompt_tokens += prompt
    runtime.total_completion_tokens += completion
    record_stage_usage(cost_cny=cost, prompt_tokens=prompt, completion_tokens=completion)


_NODE_OUTPUTS: dict[str, dict[str, Any]] = {
    "convert": {"sources_md": ["work/sources_md/a.md"], "source_ref_manifests": []},
    "caption": {"caption_results": []},
    "structure": {
        "proposed_structure": "work/structure/proposed-structure.yaml",
        "approved_structure": "work/structure/approved-structure.yaml",
    },
    "split": {
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "chapter_titles": {"chapter-1": "Intro"},
        "chapter_topics": {"chapter-1": ["t"]},
        "chapter_alignment": "work/chapter_sources/_alignment.json",
        "chapter_split_report": "work/logs/split.json",
    },
    "build_skeleton": {"skeleton": "work/skeleton.json"},
    "generate": {"agent_results": {"chapter-1": {"chapter": "x.json"}}},
    "reconcile_concepts": {"reconciled_concepts": "r.json", "alias_map": "m.json"},
    "concept_pages": {"concept_pages": []},
    "integrate": {"content_ready": True, "content_index": "content/docs/index.mdx"},
    "check": {"check_report": "work/logs/check-report.json", "repair_targets": []},
    "repair": {"repairs": [], "repair_targets": []},
    "index": {"sqlite": "site/.bookwiki/bookwiki.sqlite"},
}


def _register_fakes(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    for name, output in _NODE_OUTPUTS.items():

        def make(node_name: str, payload: dict[str, Any]):
            def node(state: dict[str, Any], cfg: Any) -> dict[str, Any]:
                calls.append(node_name)
                return {**payload, "cache_hit": False}

            return node

        monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, name, make(name, output))


def _manifest(cfg: Any) -> dict[str, Any]:
    return json.loads((cfg.work_dir / "logs" / "run-manifest.json").read_text(encoding="utf-8"))


def test_fresh_run_pauses_before_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, resume=False)

    assert calls == ["convert", "caption", "structure"]
    assert "split" not in calls
    manifest = _manifest(cfg)
    assert manifest["status"] == "paused"
    assert manifest["next_node"] == "split"


def test_stop_after_runs_only_up_to_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, stop_after="convert", resume=False)

    assert calls == ["convert"]
    manifest = _manifest(cfg)
    assert manifest["status"] == "paused"
    assert manifest["next_node"] == "caption"


def test_manifest_records_llm_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = SimpleNamespace(
        total_cost_cny=0.0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    def fake_convert(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        _spend(cfg_arg.llm_runtime, cost=1.2345678, prompt=120, completion=34)
        return {**_NODE_OUTPUTS["convert"], "cache_hit": False}

    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "convert", fake_convert)

    run_pipeline(cfg, stop_after="convert", resume=False)

    assert _manifest(cfg)["llm_usage"] == {
        "currency": "CNY",
        "total_cost_cny": 1.234568,
        "prompt_tokens": 120,
        "completion_tokens": 34,
        "total_tokens": 154,
        "budget_max_cost_cny": 70.0,
        "runs": [
            {
                "convert": {
                    "cost_cny": 1.234568,
                    "prompt_tokens": 120,
                    "completion_tokens": 34,
                    "total_tokens": 154,
                }
            }
        ],
    }


def test_manifest_records_stage_llm_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = SimpleNamespace(
        total_cost_cny=0.0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )

    def fake_convert(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        _spend(cfg_arg.llm_runtime, cost=0.25, prompt=100, completion=20)
        return {**_NODE_OUTPUTS["convert"], "cache_hit": False}

    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "convert", fake_convert)

    run_pipeline(cfg, stop_after="convert", resume=False)

    usage = _manifest(cfg)["llm_usage"]
    assert usage["total_cost_cny"] == 0.25
    assert usage["total_tokens"] == 120
    assert usage["runs"] == [
        {
            "convert": {
                "cost_cny": 0.25,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            }
        }
    ]


def test_manifest_accumulates_llm_usage_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = SimpleNamespace(
        total_cost_cny=0.0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )

    def fake_convert(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        _spend(cfg_arg.llm_runtime, cost=0.25, prompt=100, completion=20)
        return {**_NODE_OUTPUTS["convert"], "cache_hit": False}

    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "convert", fake_convert)

    run_pipeline(cfg, stop_after="convert", resume=False)
    run_pipeline(cfg, stop_after="convert", resume=False)

    usage = _manifest(cfg)["llm_usage"]
    assert usage["total_cost_cny"] == 0.5
    assert usage["prompt_tokens"] == 200
    assert usage["completion_tokens"] == 40
    assert usage["total_tokens"] == 240
    # Each run is its own object keyed by stage name; runs stay grouped, not merged.
    assert usage["runs"] == [
        {
            "convert": {
                "cost_cny": 0.25,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            }
        },
        {
            "convert": {
                "cost_cny": 0.25,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            }
        },
    ]


def test_manifest_is_written_when_run_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = SimpleNamespace(
        total_cost_cny=0.0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )

    def fake_convert(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        _spend(cfg_arg.llm_runtime, cost=0.25, prompt=100, completion=20)
        raise asyncio.CancelledError

    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "convert", fake_convert)

    with pytest.raises(asyncio.CancelledError):
        run_pipeline(cfg, stop_after="convert", resume=False)

    manifest = _manifest(cfg)
    assert manifest["status"] == "interrupted"
    assert manifest["llm_usage"] == {
        "currency": "CNY",
        "total_cost_cny": 0.25,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "budget_max_cost_cny": 70.0,
        "runs": [
            {
                "convert": {
                    "cost_cny": 0.25,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "status": "interrupted",
                }
            }
        ],
    }


def test_pause_after_halts_after_listed_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, pause_after=["caption"], resume=False)

    assert calls == ["convert", "caption"]
    assert _manifest(cfg)["next_node"] == "structure"


def test_resume_completes_pipeline_past_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, resume=False)  # pauses before split
    calls.clear()
    state = run_pipeline(cfg, resume=True)  # continues to index

    assert calls[0] == "split"
    assert calls[-1] == "index"
    assert state["sqlite"] == "site/.bookwiki/bookwiki.sqlite"
    assert _manifest(cfg)["status"] == "completed"


def test_completed_checkpoint_short_circuits_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, resume=False)
    run_pipeline(cfg, resume=True)
    calls.clear()
    capsys.readouterr()

    state = run_pipeline(cfg, resume=True)

    assert calls == []
    assert state["sqlite"] == "site/.bookwiki/bookwiki.sqlite"
    assert "completed checkpoint found" in capsys.readouterr().out


def test_force_from_reruns_from_target_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, resume=False)
    run_pipeline(cfg, resume=True)
    calls.clear()

    cfg.force_from = "integrate"
    run_pipeline(cfg, resume=False)

    assert "convert" not in calls
    assert "generate" not in calls
    assert calls[0] == "integrate"
    assert "index" in calls


def test_generate_runs_as_langgraph_fanout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []

    for name, output in _NODE_OUTPUTS.items():
        if name == "generate":
            continue

        def make(node_name: str, payload: dict[str, Any]):
            def node(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
                calls.append(node_name)
                if node_name == "split":
                    return {
                        **payload,
                        "chapter_sources": {
                            "chapter-1": "work/chapter_sources/chapter-1/source.md",
                            "chapter-2": "work/chapter_sources/chapter-2/source.md",
                            "chapter-3": "work/chapter_sources/chapter-3/source.md",
                        },
                        "cache_hit": False,
                    }
                return {**payload, "cache_hit": False}

            return node

        monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, name, make(name, output))

    probe = {"current": 0, "max": 0}

    async def fake_generate_chapter(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        ch_id = str(state["_fanout_chapter_id"])
        probe["current"] += 1
        probe["max"] = max(probe["max"], probe["current"])
        try:
            await asyncio.sleep(0.01)
            return {
                "_generate_parts": {
                    ch_id: {
                        "chapter_id": ch_id,
                        "agent_results": {"chapter": f"work/agent_results/{ch_id}.chapter.json"},
                        "generation_issues": [],
                        "generated_figures": {},
                        "cache_hit": True,
                    }
                }
            }
        finally:
            probe["current"] -= 1

    monkeypatch.setattr(nodes_module, "generate_chapter_fanout_node", fake_generate_chapter)

    run_pipeline(cfg, resume=False)
    state = run_pipeline(cfg, stop_after="generate", resume=True)

    assert calls[:4] == ["convert", "caption", "structure", "split"]
    assert set(state["agent_results"]) == {"chapter-1", "chapter-2", "chapter-3"}
    assert probe["max"] > 1


def test_generate_langgraph_fanout_can_target_one_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []

    for name, output in _NODE_OUTPUTS.items():
        if name == "generate":
            continue

        def make(node_name: str, payload: dict[str, Any]):
            def node(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
                calls.append(node_name)
                if node_name == "split":
                    return {
                        **payload,
                        "chapter_sources": {
                            "chapter-1": "work/chapter_sources/chapter-1/source.md",
                            "chapter-2": "work/chapter_sources/chapter-2/source.md",
                        },
                        "cache_hit": False,
                    }
                return {**payload, "cache_hit": False}

            return node

        monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, name, make(name, output))

    generated_ids: list[str] = []

    async def fake_generate_chapter(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        ch_id = str(state["_fanout_chapter_id"])
        generated_ids.append(ch_id)
        figures = {"chapter-2": {"fig-2": '<BookFigure id="fig-2" src="/b.png" />'}}.get(ch_id, {})
        return {
            "_generate_parts": {
                ch_id: {
                    "chapter_id": ch_id,
                    "agent_results": {"chapter": f"work/agent_results/{ch_id}.chapter.new.json"},
                    "generation_issues": [],
                    "generated_figures": figures,
                    "cache_hit": False,
                }
            }
        }

    monkeypatch.setattr(nodes_module, "generate_chapter_fanout_node", fake_generate_chapter)

    run_pipeline(cfg, resume=False)
    initial = run_pipeline(cfg, stop_after="generate", resume=True)
    assert generated_ids == ["chapter-1", "chapter-2"]
    assert initial["generated_figures"] == {
        "chapter-2": {"fig-2": '<BookFigure id="fig-2" src="/b.png" />'}
    }
    generated_ids.clear()

    cfg.force_from = "generate"
    cfg.target_chapters = ["chapter-2"]
    state = run_pipeline(cfg, stop_after="generate", resume=False)

    assert generated_ids == ["chapter-2"]
    assert state["agent_results"]["chapter-1"] == initial["agent_results"]["chapter-1"]
    assert state["generated_figures"] == initial["generated_figures"]
    assert set(state["agent_results"]) == {"chapter-1", "chapter-2"}


def test_concept_pages_run_as_langgraph_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []

    for name, output in _NODE_OUTPUTS.items():
        if name == "concept_pages":
            continue

        def make(node_name: str, payload: dict[str, Any]):
            def node(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
                calls.append(node_name)
                if node_name == "reconcile_concepts":
                    concepts_path = cfg_arg.work_dir / "concepts" / "reconciled.json"
                    concepts_path.parent.mkdir(parents=True, exist_ok=True)
                    concepts_path.write_text(
                        json.dumps(
                            {
                                "concepts": [
                                    {"canonical": "递归", "aliases": [], "source_chapter_ids": []},
                                    {
                                        "canonical": "动态规划",
                                        "aliases": [],
                                        "source_chapter_ids": [],
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    return {
                        "reconciled_concepts": "work/concepts/reconciled.json",
                        "alias_map": "work/concepts/alias_map.json",
                        "cache_hit": False,
                    }
                return {**payload, "cache_hit": False}

            return node

        monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, name, make(name, output))

    probe = {"current": 0, "max": 0}

    async def fake_concept_page(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        item = dict(state["_fanout_concept_item"])
        name = str(item["canonical"])
        order = int(state["_fanout_concept_order"])
        probe["current"] += 1
        probe["max"] = max(probe["max"], probe["current"])
        try:
            await asyncio.sleep(0.01)
            return {
                "_concept_page_parts": {
                    name: {
                        "name": name,
                        "order": order,
                        "path": f"work/agent_results/concepts/{state['_fanout_concept_stem']}.json",
                        "concept_generation_issues": [],
                        "cache_hit": True,
                    }
                }
            }
        finally:
            probe["current"] -= 1

    monkeypatch.setattr(nodes_module, "concept_page_fanout_node", fake_concept_page)

    run_pipeline(cfg, resume=False)
    state = run_pipeline(cfg, stop_after="concept_pages", resume=True)

    assert state["concept_pages"] == {
        "递归": "work/agent_results/concepts/递归.json",
        "动态规划": "work/agent_results/concepts/动态规划.json",
    }
    assert probe["max"] > 1


def test_concurrent_fanout_attributes_usage_per_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent ``concept_page`` siblings roll up into one aggregated stage row
    whose total is the plain sum of each sibling's own usage.

    Regression for the run-manifest bug where diffing a shared global counter across
    overlapping before/after windows made every sibling log the running total, so
    summing the stages inflated ``total_cost_cny`` (≈ N²/2 over-count). Per-task
    attribution + per-stage aggregation now yields the exact per-node total.
    """
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.llm_runtime = SimpleNamespace(
        total_cost_cny=0.0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )
    costs = {"递归": 0.10, "动态规划": 0.20, "贪心": 0.30}

    for name, output in _NODE_OUTPUTS.items():
        if name == "concept_pages":
            continue

        def make(node_name: str, payload: dict[str, Any]):
            def node(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
                if node_name == "reconcile_concepts":
                    concepts_path = cfg_arg.work_dir / "concepts" / "reconciled.json"
                    concepts_path.parent.mkdir(parents=True, exist_ok=True)
                    concepts_path.write_text(
                        json.dumps(
                            {
                                "concepts": [
                                    {"canonical": c, "aliases": [], "source_chapter_ids": []}
                                    for c in costs
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    return {
                        "reconciled_concepts": "work/concepts/reconciled.json",
                        "alias_map": "work/concepts/alias_map.json",
                        "cache_hit": False,
                    }
                return {**payload, "cache_hit": False}

            return node

        monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, name, make(name, output))

    async def fake_concept_page(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        item = dict(state["_fanout_concept_item"])
        name = str(item["canonical"])
        order = int(state["_fanout_concept_order"])
        # Yield first so every sibling is in flight before any of them spends:
        # this is exactly the interleaving that made the old global-diff over-count.
        await asyncio.sleep(0.01)
        _spend(cfg_arg.llm_runtime, cost=costs[name], prompt=100, completion=20)
        return {
            "_concept_page_parts": {
                name: {
                    "name": name,
                    "order": order,
                    "path": f"work/agent_results/concepts/{state['_fanout_concept_stem']}.json",
                    "concept_generation_issues": [],
                    "cache_hit": True,
                }
            }
        }

    monkeypatch.setattr(nodes_module, "concept_page_fanout_node", fake_concept_page)

    run_pipeline(cfg, resume=False)
    run_pipeline(cfg, stop_after="concept_pages", resume=True)

    usage = _manifest(cfg)["llm_usage"]
    page_entries = [run["concept_page"] for run in usage["runs"] if "concept_page" in run]
    # The fixed node collapses all concurrent siblings into one aggregated entry whose
    # total is the plain sum of each sibling's own spend — not a cumulative snapshot
    # summed across siblings (the old bug would yield ~N x the real total here).
    assert len(page_entries) == 1
    assert page_entries[0]["cost_cny"] == pytest.approx(sum(costs.values()))
    assert page_entries[0]["completion_tokens"] == 20 * len(costs)
    assert usage["total_cost_cny"] == pytest.approx(sum(costs.values()))
    assert cfg.llm_runtime.total_cost_cny == pytest.approx(sum(costs.values()))


def test_concept_pages_langgraph_fanout_can_target_one_concept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")

    for name, output in _NODE_OUTPUTS.items():
        if name == "concept_pages":
            continue

        def make(node_name: str, payload: dict[str, Any]):
            def node(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
                if node_name == "reconcile_concepts":
                    concepts_path = cfg_arg.work_dir / "concepts" / "reconciled.json"
                    concepts_path.parent.mkdir(parents=True, exist_ok=True)
                    concepts_path.write_text(
                        json.dumps(
                            {
                                "concepts": [
                                    {"canonical": "递归", "aliases": [], "source_chapter_ids": []},
                                    {
                                        "canonical": "动态规划",
                                        "aliases": [],
                                        "source_chapter_ids": [],
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    return {
                        "reconciled_concepts": "work/concepts/reconciled.json",
                        "alias_map": "work/concepts/alias_map.json",
                        "cache_hit": False,
                    }
                return {**payload, "cache_hit": False}

            return node

        monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, name, make(name, output))

    generated_names: list[str] = []

    async def fake_concept_page(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        item = dict(state["_fanout_concept_item"])
        name = str(item["canonical"])
        generated_names.append(name)
        return {
            "_concept_page_parts": {
                name: {
                    "name": name,
                    "order": int(state["_fanout_concept_order"]),
                    "path": f"work/agent_results/concepts/{state['_fanout_concept_stem']}.new.json",
                    "concept_generation_issues": [],
                    "cache_hit": False,
                }
            }
        }

    monkeypatch.setattr(nodes_module, "concept_page_fanout_node", fake_concept_page)

    run_pipeline(cfg, resume=False)
    initial = run_pipeline(cfg, stop_after="concept_pages", resume=True)
    assert generated_names == ["递归", "动态规划"]
    generated_names.clear()

    cfg.force_from = "concept_pages"
    cfg.target_concepts = ["动态规划"]
    state = run_pipeline(cfg, stop_after="concept_pages", resume=False)

    assert generated_names == ["动态规划"]
    assert state["concept_pages"]["递归"] == initial["concept_pages"]["递归"]
    assert state["concept_pages"]["动态规划"].endswith("动态规划.new.json")


def test_repair_loop_reintegrates_until_check_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    check_state = {"count": 0}

    def fake_check(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        calls.append("check")
        check_state["count"] += 1
        targets = ["chapter-1:quiz"] if check_state["count"] == 1 else []
        return {"check_report": "work/logs/check-report.json", "repair_targets": targets}

    def fake_repair(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        calls.append("repair")
        # A quiz repair rewrites the source artifact -> re-render via integrate.
        return {
            "repairs": ["work/repairs/chapter-1-quiz.json"],
            "repair_artifact_changed": True,
            "repair_targets": [],
        }

    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "check", fake_check)
    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "repair", fake_repair)

    run_pipeline(cfg, resume=False)
    calls.clear()
    run_pipeline(cfg, resume=True)

    assert calls.count("repair") == 1
    assert calls.count("check") == 2
    assert calls.count("integrate") == 2  # initial + re-integrate after repair
    assert calls[-1] == "index"


def test_repair_loop_revalidates_mdx_edits_without_reintegrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    check_state = {"count": 0}

    def fake_check(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        calls.append("check")
        check_state["count"] += 1
        targets = ["chapter-1:chapter"] if check_state["count"] == 1 else []
        return {"check_report": "work/logs/check-report.json", "repair_targets": targets}

    def fake_repair(state: dict[str, Any], cfg_arg: Any) -> dict[str, Any]:
        calls.append("repair")
        # An in-place .mdx edit must route to check (re-validate), NOT integrate (which would
        # regenerate the file from source and clobber the edit).
        return {
            "mdx_edited": ["chapter-1:chapter"],
            "repair_artifact_changed": False,
            "repair_targets": [],
        }

    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "check", fake_check)
    monkeypatch.setitem(nodes_module.NODE_FUNCTIONS, "repair", fake_repair)

    run_pipeline(cfg, resume=False)  # interrupts at the structure-approval gate
    calls.clear()
    run_pipeline(cfg, resume=True)

    assert calls.count("repair") == 1
    assert calls.count("check") == 2  # initial + re-validate after the .mdx edit
    assert calls.count("integrate") == 1  # NO re-integrate — the edit stays on disk
    assert calls[-1] == "index"


def test_logs_node_start_and_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)
    timestamps = iter([datetime(2026, 6, 25, 16, 0, 1), datetime(2026, 6, 25, 16, 0, 1)])

    class Clock:
        @classmethod
        def now(cls) -> datetime:
            return next(timestamps)

    monkeypatch.setattr(logging_utils, "datetime", Clock)

    with caplog.at_level(logging.INFO):
        run_pipeline(cfg, stop_after="convert", resume=False)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "node start name=convert book_id=mini" in messages
    assert "node done name=convert book_id=mini cache_hit=False" in messages

    log_files = sorted((cfg.work_dir / "logs").glob("pipeline-*.log"))
    assert len(log_files) == 1
    assert log_files[0].name == "pipeline-20260625-160001-001.log"
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "node start name=convert book_id=mini" in log_text
    assert "node done name=convert book_id=mini cache_hit=False" in log_text

    run_pipeline(cfg, stop_after="convert", resume=False)
    log_files_after_second_run = sorted((cfg.work_dir / "logs").glob("pipeline-*.log"))
    assert len(log_files_after_second_run) == 2
    assert [path.name for path in log_files_after_second_run] == [
        "pipeline-20260625-160001-001.log",
        "pipeline-20260625-160001-002.log",
    ]


def test_resume_with_stop_target_behind_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    calls: list[str] = []
    _register_fakes(monkeypatch, calls)

    run_pipeline(cfg, resume=False)  # pause before split
    run_pipeline(cfg, resume=True, pause_after=["integrate"])  # pause with next=check
    calls.clear()

    run_pipeline(cfg, resume=True, stop_after="convert")  # stop target already behind

    assert calls == []
