from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bookwiki.pipeline.nodes import NODE_FUNCTIONS
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.dry_run import summarize
from bookwiki.utils.files import ensure_dir, read_json, write_json
from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

NODE_ORDER = [
    "convert",
    "caption",
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

NODE_OUTPUT_KEYS = {
    "convert": {
        "sources_md",
        "source_ref_manifests",
    },
    "caption": {
        "caption_results",
    },
    "structure": {
        "proposed_structure",
        "approved_structure",
    },
    "split": {
        "chapter_sources",
        "chapter_titles",
        "chapter_alignment",
        "chapter_split_report",
    },
    "generate": {
        "agent_results",
    },
    "reconcile_concepts": {
        "reconciled_concepts",
        "alias_map",
    },
    "concept_pages": {
        "concept_pages",
    },
    "integrate": {
        "content_ready",
        "content_index",
    },
    "check": {
        "check_report",
        "repair_targets",
    },
    "repair": {
        "repairs",
        "repair_targets",
    },
    "index": {
        "sqlite",
    },
}


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
            "Critical path: convert -> caption -> structure -> split -> "
            "generate -> check -> index\n"
        )

    def invoke(
        self, initial_state: dict[str, Any] | None = None, *, resume: bool = False
    ) -> dict[str, Any]:
        if self.dry_run:
            return {"dry_run": True, "report": self.dry_run_report()}

        ensure_dir(self.cfg.cache_dir)
        ensure_dir(self.cfg.work_dir / "logs")

        if self.cfg.force_from:
            checkpoint = read_json(self.checkpoint_path, default={})
            checkpoint_state = checkpoint.get("state", {})
            self._clear_for_force()
            state = self._state_for_force_from(checkpoint_state)
            start_index = NODE_ORDER.index(self.cfg.force_from)
            nodes_log: list[dict[str, Any]] = []
            resumed_next_node: str | None = None
        else:
            checkpoint = read_json(self.checkpoint_path, default={})
            checkpoint_matches_config = checkpoint.get("config_hash") == self._config_hash()
            if resume and checkpoint.get("status") == "completed" and checkpoint_matches_config:
                state = checkpoint.get("state", {"book_id": self.cfg.book_id})
                print("resume: completed checkpoint found; cache_hit: true")
                return state
            if resume and checkpoint.get("state") and checkpoint_matches_config:
                state = checkpoint["state"]
                next_node = checkpoint.get("next_node")
                start_index = NODE_ORDER.index(next_node) if next_node in NODE_ORDER else 0
                resumed_next_node = next_node if next_node in NODE_ORDER else None
                nodes_log = read_json(self.manifest_path, default={}).get("nodes", [])
            elif resume and checkpoint.get("state"):
                state, start_index = self._state_after_config_change(checkpoint["state"])
                nodes_log = []
                resumed_next_node = None
            else:
                state = initial_state or {"book_id": self.cfg.book_id}
                start_index = 0
                nodes_log = []
                resumed_next_node = None

        stop_index = NODE_ORDER.index(self.stop_after) if self.stop_after in NODE_ORDER else None
        pending_repair_loop = (
            start_index == NODE_ORDER.index("repair") and bool(state.get("repair_targets"))
        )
        if stop_index is not None and start_index > stop_index and not pending_repair_loop:
            self._write_checkpoint(state, nodes_log, status="paused", next_index=start_index)
            return state

        index = start_index
        while index < len(NODE_ORDER):
            node_name = NODE_ORDER[index]
            if node_name in self.interrupt_before and resumed_next_node != node_name:
                LOGGER.info(
                    "node pause_before name=%s book_id=%s",
                    node_name,
                    self.cfg.book_id,
                )
                self._write_checkpoint(state, nodes_log, status="paused", next_index=index)
                return state
            resumed_next_node = None

            if node_name == "repair" and not state.get("repair_targets"):
                LOGGER.info(
                    "node skip name=%s book_id=%s reason=no_repair_targets",
                    node_name,
                    self.cfg.book_id,
                )
                index += 1
                continue

            fn = NODE_FUNCTIONS[node_name]
            LOGGER.info("node start name=%s book_id=%s", node_name, self.cfg.book_id)
            try:
                delta = self._run_node(fn, state)
            except Exception:
                LOGGER.exception("node error name=%s book_id=%s", node_name, self.cfg.book_id)
                raise
            state.update(delta)
            cache_hit = bool(delta.get("cache_hit", False))
            LOGGER.info(
                "node done name=%s book_id=%s cache_hit=%s",
                node_name,
                self.cfg.book_id,
                cache_hit,
            )
            nodes_log.append(
                {
                    "name": node_name,
                    "status": "completed",
                    "cache_hit": cache_hit,
                }
            )

            if node_name == "repair" and state.get("repairs"):
                integrate_index = NODE_ORDER.index("integrate")
                self._write_checkpoint(
                    state, nodes_log, status="running", next_index=integrate_index
                )
                index = integrate_index
                continue

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
        write_json(
            self.checkpoint_path,
            {
                "status": status,
                "next_node": next_node,
                "config_hash": self._config_hash(),
                "state": state,
            },
        )
        write_json(
            self.manifest_path,
            {
                "book_id": self.cfg.book_id,
                "status": status,
                "next_node": next_node,
                "config_hash": self._config_hash(),
                "nodes": nodes_log,
                "outputs": {
                    "content": str(self.cfg.content_dir),
                    "sqlite": state.get("sqlite"),
                },
            },
        )

    def _config_hash(self) -> str:
        config = self.cfg.to_json()
        config["book_notes_hash"] = hashlib.sha256(
            self.cfg.book_notes.encode("utf-8")
        ).hexdigest()
        payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _state_after_config_change(
        self, checkpoint_state: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        sources_md = checkpoint_state.get("sources_md")
        if sources_md and self.stop_after != "convert":
            source_ref_manifests = checkpoint_state.get(
                "source_ref_manifests"
            ) or self._existing_source_ref_manifests()
            if not source_ref_manifests:
                print("resume: config changed; rerunning from convert")
                return {"book_id": self.cfg.book_id}, 0
            print("resume: config changed; rerunning from caption")
            return (
                {
                    "book_id": checkpoint_state.get("book_id", self.cfg.book_id),
                    "sources_md": sources_md,
                    "source_ref_manifests": source_ref_manifests,
                },
                NODE_ORDER.index("caption"),
            )
        print("resume: config changed; rerunning from convert")
        return {"book_id": self.cfg.book_id}, 0

    def _state_for_force_from(self, checkpoint_state: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = dict(checkpoint_state) if checkpoint_state else {}
        state["book_id"] = str(state.get("book_id") or self.cfg.book_id)
        if self.cfg.force_from:
            start_index = NODE_ORDER.index(self.cfg.force_from)
            for node_name in NODE_ORDER[start_index:]:
                for key in NODE_OUTPUT_KEYS.get(node_name, set()):
                    state.pop(key, None)
        start_index = NODE_ORDER.index(self.cfg.force_from) if self.cfg.force_from else 0
        if self.cfg.force_from and start_index >= NODE_ORDER.index("caption") and not state.get(
            "sources_md"
        ):
            sources = self._existing_sources_md()
            if not sources:
                msg = (
                    f"--from {self.cfg.force_from} requires converted markdown in "
                    f"{self.cfg.work_dir / 'sources_md'}; run convert first"
                )
                raise FileNotFoundError(msg)
            state["sources_md"] = sources
        if self.cfg.force_from == "caption" and not state.get("source_ref_manifests"):
            manifests = self._existing_source_ref_manifests()
            if not manifests:
                msg = (
                    "--from caption requires source ref manifests in "
                    f"{self.cfg.work_dir / 'source_refs'}; run convert first"
                )
                raise FileNotFoundError(msg)
            state["source_ref_manifests"] = manifests
        if self.cfg.force_from and start_index >= NODE_ORDER.index("generate") and not state.get(
            "chapter_sources"
        ):
            chapter_sources, chapter_titles, alignment_path = self._existing_split_state()
            if chapter_sources:
                state["chapter_sources"] = chapter_sources
                if chapter_titles:
                    state["chapter_titles"] = chapter_titles
                if alignment_path:
                    state["chapter_alignment"] = alignment_path
        return state

    def _existing_sources_md(self) -> list[str]:
        sources_dir = self.cfg.work_dir / "sources_md"
        if not sources_dir.exists():
            return []
        return [
            path.relative_to(self.cfg.book_dir).as_posix()
            for path in sorted(sources_dir.glob("*.md"))
            if path.is_file()
        ]

    def _existing_source_ref_manifests(self) -> list[str]:
        refs_dir = self.cfg.work_dir / "source_refs"
        if not refs_dir.exists():
            return []
        return [
            path.relative_to(self.cfg.book_dir).as_posix()
            for path in sorted(refs_dir.glob("*.json"))
            if path.is_file()
        ]

    def _existing_split_state(self) -> tuple[dict[str, str], dict[str, str], str | None]:
        chapter_sources_dir = self.cfg.work_dir / "chapter_sources"
        if not chapter_sources_dir.exists():
            return {}, {}, None
        chapter_sources = {
            path.parent.name: path.relative_to(self.cfg.book_dir).as_posix()
            for path in sorted(chapter_sources_dir.glob("*/source.md"))
            if path.is_file()
        }
        alignment_path = chapter_sources_dir / "_alignment.json"
        alignment_rel = None
        chapter_titles: dict[str, str] = {}
        if alignment_path.exists():
            alignment_rel = alignment_path.relative_to(self.cfg.book_dir).as_posix()
            alignment = read_json(alignment_path, default={})
            raw_titles = alignment.get("chapter_titles", {})
            if isinstance(raw_titles, dict):
                chapter_titles = {str(key): str(value) for key, value in raw_titles.items()}
        return chapter_sources, chapter_titles, alignment_rel

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
