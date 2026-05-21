from __future__ import annotations


def find_wikilinks(markdown: str) -> list[str]:
    import re

    return re.findall(r"\[\[([^\]]+)\]\]", markdown)
