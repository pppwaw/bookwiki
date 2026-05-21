from __future__ import annotations

from pathlib import Path


def convert_pptx_to_md(path: str | Path) -> str:
    path = Path(path)
    return f"# {path.stem}\n\n<!-- source_ref: {path.stem}-slide01 -->\n\nM1 PPTX stub.\n"
