from __future__ import annotations

from bookwiki.indexer.sqlite_builder import build_sqlite_index
from bookwiki.pipeline._shared import (
    _LOG,
    State,
    _rel,
)
from bookwiki.scheduler.config import BookConfig


def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    _LOG.info("index: building sqlite db=%s", _rel(db_path, cfg.book_dir))
    build_sqlite_index(cfg.content_dir, db_path)
    size = db_path.stat().st_size if db_path.exists() else 0
    _LOG.info("index: done db_size_bytes=%d", size)
    return {"sqlite": _rel(db_path, cfg.book_dir)}


__all__ = [
    "index_node",
]
