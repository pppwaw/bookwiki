from __future__ import annotations


def normalize_wikilinks(markdown: str, alias_map: dict[str, str]) -> str:
    output = markdown
    for alias, canonical in alias_map.items():
        output = output.replace(f"[[{alias}]]", f"[[{canonical}]]")
    return output
