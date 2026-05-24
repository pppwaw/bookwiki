from __future__ import annotations

import re


def normalize_concept_links(mdx: str, alias_map: dict[str, str]) -> str:
    output = mdx
    for alias, canonical in alias_map.items():
        output = output.replace(f"[[{alias}]]", f"[[{canonical}]]")
    return output


def normalize_mdx_math(mdx: str) -> str:
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", mdx)
    return "".join(
        part if part.startswith("`") else _normalize_math_segment(part) for part in parts
    )


def _normalize_math_segment(segment: str) -> str:
    segment = re.sub(
        r"\s*\\\[([\s\S]*?)\\\]\s*[.,;:]?",
        lambda match: f"\n\n$$\n{match.group(1).strip()}\n$$\n\n",
        segment,
    )
    return re.sub(
        r"\\\(([\s\S]*?)\\\)",
        lambda match: f"${match.group(1).strip()}$",
        segment,
    )
