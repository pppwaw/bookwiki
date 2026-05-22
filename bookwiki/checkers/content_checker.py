from __future__ import annotations

from pathlib import Path


def content_has_mdx(content_dir: str | Path) -> bool:
    return any(Path(content_dir).rglob("*.mdx"))
