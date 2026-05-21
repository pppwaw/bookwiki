from __future__ import annotations


def render_chapter(title: str, body_md: str) -> str:
    return f"# {title}\n\n{body_md.strip()}\n"
