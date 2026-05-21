from __future__ import annotations

from pathlib import Path

from bookwiki.scheduler.config import load_config
from bookwiki.scheduler.graph import build_graph, resume_or_start


def rebuild_sqlite(book_dir: str | Path) -> Path:
    cfg = load_config(book_dir)
    graph = build_graph(cfg, stop_after="index")
    state = resume_or_start(graph, cfg.book_id, resume=True)
    return cfg.book_dir / state["sqlite"]
