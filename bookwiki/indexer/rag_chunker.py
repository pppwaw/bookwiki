from __future__ import annotations


def chunk_markdown(markdown: str, limit: int = 1200) -> list[str]:
    return [markdown[index : index + limit] for index in range(0, len(markdown), limit)] or [""]
