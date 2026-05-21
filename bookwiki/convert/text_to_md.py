from __future__ import annotations

from pathlib import Path


def convert_text_to_md(path: str | Path) -> str:
    path = Path(path)
    body = path.read_text(encoding="utf-8", errors="ignore")
    return f"# {path.stem}\n\n<!-- source_ref: {path.stem}-text -->\n\n{body}\n"
