from __future__ import annotations

from pathlib import Path


def convert_pdf_to_md(path: str | Path) -> str:
    path = Path(path)
    return (
        f"# {path.stem}\n\n"
        f"<!-- source_ref: {path.stem}-p001 -->\n\n"
        f"M1 PDF conversion stub for `{path.name}` ({path.stat().st_size} bytes).\n"
    )
