from __future__ import annotations


def render_concept(name: str, body_md: str) -> str:
    return f"# {name}\n\n{body_md.strip()}\n"
