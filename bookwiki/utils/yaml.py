from __future__ import annotations

from typing import Any


def dump_frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in data.items())
    lines.append("---")
    return "\n".join(lines) + "\n"
