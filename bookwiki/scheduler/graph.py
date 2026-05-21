from __future__ import annotations

import asyncio
import inspect
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bookwiki.pipeline.nodes import NODE_FUNCTIONS
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.dry_run import summarize
from bookwiki.utils.files import ensure_dir, read_json, write_json

NODE_ORDER = [
    "convert",
    "structure",
    "split",
    "generate",
    "reconcile_concepts",
    "concept_pages",
    "integrate",
    "check",
    "repair",
    "index",
]


class GraphView:
    def draw_mermaid(self) -> str:
        lines = ["graph TD", "    START --> convert"]
        for left, right in zip(NODE_ORDER, NODE_ORDER[1:], strict=False):
            if left == "check":
                lines.append("    check -->|issues| repair")
                lines.append("    check -->|clean| index")
            elif left == "repair":
                lines.append("    repair --> integrate")
            elif right != "repair":
                lines.append(f"    {left} --> {right}")
        lines.append("    index --> END")
        return "\n".join(lines)


@dataclass
class BookGraph:
    cfg: BookConfig
    stop_after: str | None = None
    pause_after: list[str] = field(default_factory=list)
    dry_run: bool = False
    interrupt_before: list[str] = field(default_factory=lambda: ["split"])

    @property
    def checkpoint_path(self) -> Path:
        return self.cfg.cache_dir / "checkpoint.json"

    @property
    def manifest_path(self) -> Path:
        return self.cfg.work_dir / "logs" / "run-manifest.json"

    def get_graph(self) -> GraphView:
        return GraphView()

    def dry_run_report(self) -> str:
        chapter_count = 2
        current = read_json(self.checkpoint_path, default={})
        chapters = current.get("state", {}).get("chapter_sources", {})
        if chapters:
            chapter_count = len(chapters)
        estimate = summarize(NODE_ORDER, chapter_count=chapter_count)
        return (
            f"{self.get_graph().draw_mermaid()}\n\n"
            f"Estimated tokens: {estimate.tokens}\n"
            f"Estimated cost USD: {estimate.cost_usd:.6f}\n"
            "Critical path: convert -> structure -> split -> generate -> check -> index\n"
        )

    def invoke(
        self, initial_state: dict[str, Any] | None = None, *, resume: bool = False
    ) -> dict[str, Any]:
        if self.dry_run:
            return {"dry_run": True, "report": self.dry_run_report()}

        ensure_dir(self.cfg.cache_dir)
        ensure_dir(self.cfg.work_dir / "logs")

        if self.cfg.force_from:
            self._clear_for_force()
            state: dict[str, Any] = {"book_id": self.cfg.book_id}
            start_index = NODE_ORDER.index(self.cfg.force_from)
            nodes_log: list[dict[str, Any]] = []
        else:
            checkpoint = read_json(self.checkpoint_path, default={})
            if resume and checkpoint.get("status") == "completed":
                state = checkpoint.get("state", {"book_id": self.cfg.book_id})
                print("resume: completed checkpoint found; cache_hit: true")
                return state
            if resume and checkpoint.get("state"):
                state = checkpoint["state"]
                next_node = checkpoint.get("next_node")
                start_index = NODE_ORDER.index(next_node) if next_node in NODE_ORDER else 0
                nodes_log = read_json(self.manifest_path, default={}).get("nodes", [])
            else:
                state = initial_state or {"book_id": self.cfg.book_id}
                start_index = 0
                nodes_log = []

        stop_index = NODE_ORDER.index(self.stop_after) if self.stop_after in NODE_ORDER else None

        index = start_index
        while index < len(NODE_ORDER):
            node_name = NODE_ORDER[index]
            if node_name == "repair" and not state.get("repair_targets"):
                index += 1
                continue

            fn = NODE_FUNCTIONS[node_name]
            delta = self._run_node(fn, state)
            state.update(delta)
            nodes_log.append(
                {
                    "name": node_name,
                    "status": "completed",
                    "cache_hit": bool(delta.get("cache_hit", False)),
                }
            )

            if node_name in self.pause_after or (stop_index is not None and index >= stop_index):
                self._write_checkpoint(state, nodes_log, status="paused", next_index=index + 1)
                return state

            self._write_checkpoint(state, nodes_log, status="running", next_index=index + 1)
            index += 1

        self._write_checkpoint(state, nodes_log, status="completed", next_index=None)
        return state

    def _run_node(self, fn: Any, state: dict[str, Any]) -> dict[str, Any]:
        result = fn(state, self.cfg)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def _write_checkpoint(
        self,
        state: dict[str, Any],
        nodes_log: list[dict[str, Any]],
        *,
        status: str,
        next_index: int | None,
    ) -> None:
        next_node = (
            NODE_ORDER[next_index]
            if next_index is not None and next_index < len(NODE_ORDER)
            else None
        )
        write_json(self.checkpoint_path, {"status": status, "next_node": next_node, "state": state})
        write_json(
            self.manifest_path,
            {
                "book_id": self.cfg.book_id,
                "status": status,
                "next_node": next_node,
                "nodes": nodes_log,
                "outputs": {
                    "vault": str(self.cfg.vault_dir),
                    "sqlite": state.get("sqlite"),
                },
            },
        )

    def _clear_for_force(self) -> None:
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
        tasks = self.cfg.cache_dir / "tasks"
        if tasks.exists():
            shutil.rmtree(tasks)


def build_graph(
    cfg: BookConfig,
    stop_after: str | None = None,
    pause_after: list[str] | None = None,
    dry_run: bool = False,
) -> BookGraph:
    cfg.pause_after = pause_after or []
    cfg.dry_run = dry_run
    return BookGraph(cfg=cfg, stop_after=stop_after, pause_after=cfg.pause_after, dry_run=dry_run)


def resume_or_start(graph: BookGraph, book_id: str, *, resume: bool = False) -> dict[str, Any]:
    state = graph.invoke({"book_id": book_id}, resume=resume)
    if state.get("dry_run"):
        print(state["report"])
    return state
