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

from bookwiki.pipeline.nodes import NODE_FUNCTIONS
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.resume import (
    NODE_ORDER,
    clear_for_force,
    config_hash,
    dry_run_report,
    state_after_config_change,
    state_for_force_from,
)
from bookwiki.scheduler.state import PipelineState
from bookwiki.utils.files import write_json
from bookwiki.utils.logging import get_logger

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
        result = fn(state, cfg)
        if inspect.isawaitable(result):
            result = await result
        cache_hit = bool((result or {}).get("cache_hit", False))
        LOGGER.info("node done name=%s book_id=%s cache_hit=%s", name, cfg.book_id, cache_hit)
        return result

    return node


def _route_after_check(state: PipelineState) -> str:
    return "repair" if state.get("repair_targets") else "index"


def _route_after_repair(state: PipelineState) -> str:
    return "integrate" if state.get("repairs") else "index"


def build_graph_def(cfg: BookConfig) -> StateGraph:
    """Build the uncompiled ``StateGraph`` mirroring the legacy node topology."""
    graph = StateGraph(PipelineState)
    for name in NODE_ORDER:
        graph.add_node(name, _bind_node(name, NODE_FUNCTIONS[name], cfg))

    graph.add_edge(START, "convert")
    graph.add_edge("convert", "caption")
    graph.add_edge("caption", "structure")
    graph.add_edge("structure", "split")
    graph.add_edge("split", "build_skeleton")
    graph.add_edge("build_skeleton", "generate")
    graph.add_edge("generate", "reconcile_concepts")
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
) -> None:
    write_json(
        cfg.work_dir / "logs" / "run-manifest.json",
        {
            "book_id": cfg.book_id,
            "status": status,
            "next_node": next_node,
            "config_hash": config_hash,
            "nodes": nodes_log,
            "outputs": {
                "content": str(cfg.content_dir),
                "sqlite": state.get("sqlite"),
            },
        },
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
async def _drive(
    graph: Any,
    input_state: dict[str, Any] | None,
    thread: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str | None]:
    nodes_log: list[dict[str, Any]] = []
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
        values, nodes_log, status, next_node = await _drive(graph, input_state, thread)
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
    (cfg.work_dir / "logs").mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        _run(cfg, stop_after=stop_after, pause_after=pause_after or [], resume=resume)
    )
