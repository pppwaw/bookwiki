from __future__ import annotations


def normalize_concept_links(mdx: str, alias_map: dict[str, str]) -> str:
    output = mdx
    for alias, canonical in alias_map.items():
        output = output.replace(f"[[{alias}]]", f"[[{canonical}]]")
    return output
