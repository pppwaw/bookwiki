from __future__ import annotations


def has_quiz(markdown: str) -> bool:
    return "## Quiz" in markdown
