from __future__ import annotations

from pathlib import Path


def markdown_title(path: str | Path) -> str:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return next(
        (line.lstrip("# ").strip() for line in lines if line.startswith("#")), Path(path).stem
    )
