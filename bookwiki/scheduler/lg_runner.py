"""LangGraph-based pipeline runner (the pipeline's single execution engine).

This module compiles the 11-stage book pipeline as a ``StateGraph`` and drives it
through an ``AsyncSqliteSaver`` checkpointer — the single source of truth for
control flow and resumable state (no ``checkpoint.json``). The CLI flags map to
LangGraph mechanisms:

* ``--resume``       continue from the checkpointed next node
* ``--from X --force`` clear caches, reconstruct state from disk, rerun from X
* ``--to Y``         stop after node Y (dynamic ``interrupt_after``)
* ``--pause-after``  pause after the listed nodes (dynamic ``interrupt_after``)
* ``--dry-run``      print the graph + cost estimate without writing

The structure-review gate is the compiled ``interrupt_before=["split"]`` pause.
State reconstruction (``--from`` / config-change salvage) and the cost/dry-run
report live as pure functions in :mod:`bookwiki.scheduler.resume`.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from bookwiki.pipeline import nodes as pipeline_nodes
from bookwiki.pipeline.nodes import NODE_FUNCTIONS
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import build_runtime
from bookwiki.scheduler.resume import (
    NODE_ORDER,
    clear_for_force,
    config_hash,
    dry_run_report,
    state_after_config_change,
    state_for_force_from,
)
from bookwiki.scheduler.state import PipelineState
from bookwiki.utils.files import read_json, write_json
from bookwiki.utils.logging import configure_book_file_logging, get_logger

LOGGER = get_logger(__name__)

CHECKPOINT_DB = "checkpoint.sqlite"
INTERRUPT_BEFORE = ["split"]
RECURSION_LIMIT = 50


# --------------------------------------------------------------------------- #
# Graph definition
# --------------------------------------------------------------------------- #
def _bind_node(name: str, fn: Any, cfg: BookConfig):
    async def node(state: PipelineState) -> dict[str, Any]:
        LOGGER.info("node start name=%s book_id=%s", name, cfg.book_id)
        cfg._llm_active_node = name
        usage_before = _llm_usage_totals(cfg)
        result = fn(state, cfg)
        if inspect.isawaitable(result):
            result = await result
        cache_hit = bool((result or {}).get("cache_hit", False))
        _append_stage_usage(cfg, name, usage_before, _llm_usage_totals(cfg))
        LOGGER.info("node done name=%s book_id=%s cache_hit=%s", name, cfg.book_id, cache_hit)
        if getattr(cfg, "_llm_active_node", None) == name:
            cfg._llm_active_node = None
        return result

    return node


def _route_after_check(state: PipelineState) -> str:
    return "repair" if state.get("repair_targets") else "index"


def _route_after_repair(state: PipelineState) -> str:
    return "integrate" if state.get("repairs") else "index"


def _send_generate_chapters_for(cfg: BookConfig):
    def route(state: PipelineState) -> list[Send] | str:
        specs = pipeline_nodes.generate_fanout_specs(state, cfg)
        if not specs:
            return "generate"
        return [Send("generate_chapter", spec) for spec in specs]

    return route


def _send_concept_pages_for(cfg: BookConfig):
    def route(state: PipelineState) -> list[Send] | str:
        specs = pipeline_nodes.concept_page_fanout_specs(state, cfg)
        if not specs:
            return "concept_pages"
        return [Send("concept_page", spec) for spec in specs]

    return route


def build_graph_def(cfg: BookConfig) -> StateGraph:
    """Build the uncompiled ``StateGraph`` mirroring the legacy node topology."""
    graph = StateGraph(PipelineState)
    use_generate_fanout = NODE_FUNCTIONS["generate"] is pipeline_nodes.generate_node
    use_concept_fanout = NODE_FUNCTIONS["concept_pages"] is pipeline_nodes.concept_pages_node
    for name in NODE_ORDER:
        fn = NODE_FUNCTIONS[name]
        if name == "generate" and use_generate_fanout:
            fn = pipeline_nodes.collect_generate_fanout_node
        elif name == "concept_pages" and use_concept_fanout:
            fn = pipeline_nodes.collect_concept_pages_fanout_node
        graph.add_node(name, _bind_node(name, fn, cfg))
    if use_generate_fanout:
        graph.add_node(
            "prepare_generate",
            _bind_node("prepare_generate", pipeline_nodes.prepare_generate_fanout_node, cfg),
        )
        graph.add_node(
            "generate_chapter",
            _bind_node("generate_chapter", pipeline_nodes.generate_chapter_fanout_node, cfg),
        )
    if use_concept_fanout:
        graph.add_node(
            "prepare_concept_pages",
            _bind_node(
                "prepare_concept_pages", pipeline_nodes.prepare_concept_pages_fanout_node, cfg
            ),
        )
        graph.add_node(
            "concept_page",
            _bind_node("concept_page", pipeline_nodes.concept_page_fanout_node, cfg),
        )

    graph.add_edge(START, "convert")
    graph.add_edge("convert", "caption")
    graph.add_edge("caption", "structure")
    graph.add_edge("structure", "split")
    graph.add_edge("split", "build_skeleton")
    if use_generate_fanout:
        graph.add_edge("build_skeleton", "prepare_generate")
        graph.add_conditional_edges("prepare_generate", _send_generate_chapters_for(cfg))
        graph.add_edge("generate_chapter", "generate")
    else:
        graph.add_edge("build_skeleton", "generate")
    graph.add_edge("generate", "reconcile_concepts")
    if use_concept_fanout:
        graph.add_edge("reconcile_concepts", "prepare_concept_pages")
        graph.add_conditional_edges("prepare_concept_pages", _send_concept_pages_for(cfg))
        graph.add_edge("concept_page", "concept_pages")
    else:
        graph.add_edge("reconcile_concepts", "concept_pages")
    graph.add_edge("concept_pages", "integrate")
    graph.add_edge("integrate", "check")
    graph.add_conditional_edges("check", _route_after_check, {"repair": "repair", "index": "index"})
    graph.add_conditional_edges(
        "repair", _route_after_repair, {"integrate": "integrate", "index": "index"}
    )
    graph.add_edge("index", END)
    return graph


def _compile(cfg: BookConfig, saver: AsyncSqliteSaver, *, interrupt_after: list[str] | None = None):
    return build_graph_def(cfg).compile(
        checkpointer=saver,
        interrupt_before=INTERRUPT_BEFORE,
        interrupt_after=interrupt_after or [],
    )


def _thread_config(cfg: BookConfig, config_hash: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": cfg.book_id},
        "metadata": {"config_hash": config_hash},
        "recursion_limit": RECURSION_LIMIT,
    }


# --------------------------------------------------------------------------- #
# Checkpoint helpers
# --------------------------------------------------------------------------- #
def _delete_checkpoint_db(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()


async def _peek(
    db_path: Path, cfg: BookConfig
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    if not db_path.exists():
        return {}, {}, None
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = _compile(cfg, saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": cfg.book_id}})
        values = dict(snapshot.values) if snapshot.values else {}
        metadata = dict(snapshot.metadata) if snapshot.metadata else {}
        next_node = snapshot.next[0] if snapshot.next else None
        return values, metadata, next_node


def _write_manifest(
    cfg: BookConfig,
    state: dict[str, Any],
    nodes_log: list[dict[str, Any]],
    *,
    status: str,
    next_node: str | None,
    config_hash: str,
    include_unfinished_usage: bool = False,
) -> None:
    manifest_path = cfg.work_dir / "logs" / "run-manifest.json"
    prior = read_json(manifest_path, default={})
    write_json(
        manifest_path,
        {
            "book_id": cfg.book_id,
            "status": status,
            "next_node": next_node,
            "config_hash": config_hash,
            "nodes": nodes_log,
            "llm_usage": _accumulated_llm_usage(
                prior,
                _llm_usage_snapshot(cfg, include_unfinished=include_unfinished_usage),
                cfg,
            ),
            "outputs": {"content": str(cfg.content_dir), "sqlite": state.get("sqlite")},
        },
    )


def _llm_usage_snapshot(cfg: BookConfig, *, include_unfinished: bool = False) -> dict[str, Any]:
    stages = list(getattr(cfg, "_llm_stage_usage", []))
    if include_unfinished:
        unfinished = _unfinished_stage_usage(cfg, stages)
        if unfinished["total_tokens"] or unfinished["cost_cny"]:
            stage_name = getattr(cfg, "_llm_active_node", None) or "interrupted"
            stages.append(
                {"name": stage_name, "status": "interrupted", "currency": "CNY", **unfinished}
            )
    totals = _sum_stage_usage(stages)
    return {
        "currency": "CNY",
        "total_cost_cny": totals["cost_cny"],
        "prompt_tokens": totals["prompt_tokens"],
        "completion_tokens": totals["completion_tokens"],
        "total_tokens": totals["total_tokens"],
        "budget_max_cost_cny": cfg.budget.get("maxCostCny"),
        "stages": stages,
    }


def _accumulated_llm_usage(
    prior_manifest: Any, current: dict[str, Any], cfg: BookConfig
) -> dict[str, Any]:
    prior_usage = prior_manifest.get("llm_usage", {}) if isinstance(prior_manifest, dict) else {}
    prior_stages = prior_usage.get("stages", []) if isinstance(prior_usage, dict) else []
    if not isinstance(prior_stages, list):
        prior_stages = []
    prompt_tokens = _usage_int(prior_usage, "prompt_tokens") + int(current["prompt_tokens"])
    completion_tokens = _usage_int(prior_usage, "completion_tokens") + int(
        current["completion_tokens"]
    )
    return {
        "currency": "CNY",
        "total_cost_cny": round(
            _usage_float(prior_usage, "total_cost_cny") + float(current["total_cost_cny"]), 6
        ),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "budget_max_cost_cny": cfg.budget.get("maxCostCny"),
        "stages": [*prior_stages, *current["stages"]],
    }


def _sum_stage_usage(stages: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_tokens = sum(int(stage.get("prompt_tokens", 0) or 0) for stage in stages)
    completion_tokens = sum(int(stage.get("completion_tokens", 0) or 0) for stage in stages)
    return {
        "cost_cny": round(sum(float(stage.get("cost_cny", 0.0) or 0.0) for stage in stages), 6),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _unfinished_stage_usage(cfg: BookConfig, stages: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = getattr(cfg, "_llm_run_usage_start", None)
    if not isinstance(baseline, dict):
        return {"cost_cny": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    current = _llm_usage_totals(cfg)
    completed = _sum_stage_usage(stages)
    prompt_tokens = (
        current["prompt_tokens"] - baseline["prompt_tokens"] - completed["prompt_tokens"]
    )
    completion_tokens = (
        current["completion_tokens"]
        - baseline["completion_tokens"]
        - completed["completion_tokens"]
    )
    prompt_tokens = max(prompt_tokens, 0)
    completion_tokens = max(completion_tokens, 0)
    return {
        "cost_cny": max(
            round(current["cost_cny"] - baseline["cost_cny"] - completed["cost_cny"], 6), 0.0
        ),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _usage_int(usage: Any, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    return int(usage.get(key, 0) or 0)


def _usage_float(usage: Any, key: str) -> float:
    if not isinstance(usage, dict):
        return 0.0
    return float(usage.get(key, 0.0) or 0.0)


def _llm_usage_totals(cfg: BookConfig) -> dict[str, Any]:
    runtime = cfg.llm_runtime
    prompt_tokens = int(getattr(runtime, "total_prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(runtime, "total_completion_tokens", 0) or 0)
    return {
        "cost_cny": round(float(getattr(runtime, "total_cost_cny", 0.0) or 0.0), 6),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _append_stage_usage(
    cfg: BookConfig,
    name: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    stage_usage = {
        "name": name,
        "currency": "CNY",
        "cost_cny": round(after["cost_cny"] - before["cost_cny"], 6),
        "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
        "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
        "total_tokens": after["total_tokens"] - before["total_tokens"],
    }
    cfg.__dict__.setdefault("_llm_stage_usage", []).append(stage_usage)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
async def _drive(
    graph: Any,
    input_state: dict[str, Any] | None,
    thread: dict[str, Any],
    nodes_log: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str | None]:
    async for chunk in graph.astream(input_state, thread, stream_mode="updates"):
        for node_name, delta in chunk.items():
            if node_name not in NODE_ORDER:
                continue
            cache_hit = bool((delta or {}).get("cache_hit", False))
            nodes_log.append({"name": node_name, "status": "completed", "cache_hit": cache_hit})

    snapshot = await graph.aget_state(thread)
    next_node = snapshot.next[0] if snapshot.next else None
    status = "paused" if snapshot.next else "completed"
    return dict(snapshot.values), nodes_log, status, next_node


async def _run(
    cfg: BookConfig,
    *,
    stop_after: str | None,
    pause_after: list[str],
    resume: bool,
) -> dict[str, Any]:
    db_path = cfg.cache_dir / CHECKPOINT_DB
    cfg_hash = config_hash(cfg)

    # Inject one shared LLM runtime for the whole pipeline so every agent call
    # reuses a single LiteLLM Router (its tpm/rpm self-throttling and usage/cost
    # accounting are per-Router; an ad-hoc runtime per call defeats both). Built
    # lazily here after the dry-run early-return, so no-LLM stages stay cheap and
    # ``config_hash`` (computed from ``cfg.to_json()``, which excludes the runtime)
    # is unaffected.
    if cfg.llm_runtime is None:
        cfg.llm_runtime = build_runtime(max_cost_cny=cfg.budget.get("maxCostCny"))
    cfg._llm_stage_usage = []
    cfg._llm_run_usage_start = _llm_usage_totals(cfg)

    prior_values, prior_meta, prior_next = await _peek(db_path, cfg)
    config_matches = prior_meta.get("config_hash") == cfg_hash

    seed_state: dict[str, Any] | None = None
    seed_index: int | None = None
    input_state: dict[str, Any] | None = None

    if cfg.force_from:
        clear_for_force(cfg)
        _delete_checkpoint_db(db_path)
        seed_state = state_for_force_from(cfg, prior_values)
        seed_index = NODE_ORDER.index(cfg.force_from)
    elif resume and prior_values and not prior_next and config_matches:
        print("resume: completed checkpoint found; cache_hit: true")
        return prior_values
    elif resume and prior_values and config_matches:
        input_state = None  # continue from checkpointed next node
    elif resume and prior_values:
        seed_state, seed_index = state_after_config_change(cfg, prior_values, stop_after)
        _delete_checkpoint_db(db_path)
    else:
        _delete_checkpoint_db(db_path)
        input_state = {"book_id": cfg.book_id}

    if seed_index is not None:
        start_index = seed_index
    elif input_state is not None:
        start_index = 0
    else:
        start_index = NODE_ORDER.index(prior_next) if prior_next in NODE_ORDER else 0

    stop_index = NODE_ORDER.index(stop_after) if stop_after in NODE_ORDER else None
    repair_state = seed_state if seed_state is not None else prior_values
    pending_repair_loop = start_index == NODE_ORDER.index("repair") and bool(
        repair_state.get("repair_targets")
    )
    if stop_index is not None and start_index > stop_index and not pending_repair_loop:
        return prior_values or seed_state or {"book_id": cfg.book_id}

    interrupt_after = list(pause_after)
    if stop_after in NODE_ORDER:
        interrupt_after.append(stop_after)

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = _compile(cfg, saver, interrupt_after=interrupt_after)
        thread = _thread_config(cfg, cfg_hash)
        if seed_state is not None and seed_index == 0:
            input_state = seed_state
        elif seed_state is not None:
            await graph.aupdate_state(
                {"configurable": {"thread_id": cfg.book_id}},
                seed_state,
                as_node=NODE_ORDER[seed_index - 1],
            )
            input_state = None
        nodes_log: list[dict[str, Any]] = []
        try:
            values, nodes_log, status, next_node = await _drive(
                graph, input_state, thread, nodes_log
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            current_task = asyncio.current_task()
            if current_task is not None:
                while current_task.cancelling():
                    current_task.uncancel()
            values: dict[str, Any] = {}
            next_node: str | None = None
            try:
                snapshot = await graph.aget_state(thread)
                values = dict(snapshot.values) if snapshot.values else {}
                next_node = snapshot.next[0] if snapshot.next else None
            except Exception:
                LOGGER.exception("failed to read checkpoint while handling interruption")
            _write_manifest(
                cfg,
                values,
                nodes_log,
                status="interrupted",
                next_node=next_node,
                config_hash=cfg_hash,
                include_unfinished_usage=True,
            )
            raise
        else:
            _write_manifest(
                cfg, values, nodes_log, status=status, next_node=next_node, config_hash=cfg_hash
            )
            return values


# --------------------------------------------------------------------------- #
# Public entry points (drop-in for scheduler.graph.build_graph/resume_or_start)
# --------------------------------------------------------------------------- #
def run_pipeline(
    cfg: BookConfig,
    *,
    stop_after: str | None = None,
    pause_after: list[str] | None = None,
    dry_run: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    cfg.pause_after = pause_after or []
    cfg.dry_run = dry_run
    if dry_run:
        return {"dry_run": True, "report": dry_run_report(cfg)}
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    configure_book_file_logging(cfg.work_dir / "logs")
    return asyncio.run(
        _run(cfg, stop_after=stop_after, pause_after=pause_after or [], resume=resume)
    )
