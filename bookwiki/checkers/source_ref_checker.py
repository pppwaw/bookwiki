from __future__ import annotations


def has_sources_section(markdown: str) -> bool:
    return "## Sources" in markdown
