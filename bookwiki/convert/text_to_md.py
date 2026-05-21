from __future__ import annotations

from pathlib import Path

from bookwiki.convert.common import clean_markdown, source_id_from_stem


def convert_text_to_md(path: str | Path, *, source_id: str | None = None) -> str:
    text_path = Path(path)
    resolved_source_id = source_id_from_stem(source_id or text_path.stem)
    body = clean_markdown(text_path.read_text(encoding="utf-8", errors="ignore"))
    return (
        f"# {text_path.stem}\n\n"
        f"<!-- source_ref: {resolved_source_id}-text -->\n\n"
        f"{body}\n"
    )
