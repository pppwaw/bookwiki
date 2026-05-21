from __future__ import annotations


def has_source_ref(markdown: str) -> bool:
    return "source_ref:" in markdown or "## Sources" in markdown
