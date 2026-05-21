from __future__ import annotations


def has_frontmatter(markdown: str) -> bool:
    return markdown.startswith("---\n")
