from __future__ import annotations

import os

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
    api_key = os.environ.get("OPENROUTER_API_KEY")
    build_sqlite_index(
        cfg.content_dir,
        db_path,
        embed=bool(api_key),
        model=os.environ.get("BOOKWIKI_EMBED_MODEL"),
        api_key=api_key,
        base_url=os.environ.get("BOOKWIKI_EMBED_BASE_URL"),
    )
    size = db_path.stat().st_size if db_path.exists() else 0
    _LOG.info("index: done db_size_bytes=%d embed=%s", size, bool(api_key))
    return {"sqlite": _rel(db_path, cfg.book_dir)}


__all__ = [
    "index_node",
]
