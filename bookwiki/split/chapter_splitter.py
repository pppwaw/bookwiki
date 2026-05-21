from __future__ import annotations

import re


def parse_approved_structure(markdown: str) -> list[tuple[str, str]]:
    matches = re.findall(r"^##\s+(ch\d+)\s+(.+)$", markdown, flags=re.MULTILINE)
    return matches or [("ch01", "Foundations"), ("ch02", "Practice")]
