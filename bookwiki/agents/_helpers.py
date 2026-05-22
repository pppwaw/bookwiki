from __future__ import annotations

import re
from typing import Any

from bookwiki.schemas.common import Citation


def chapter_id(inp: dict[str, Any]) -> str:
    return str(inp.get("chapter_id") or inp.get("chapter") or "ch01")


def chapter_title(inp: dict[str, Any]) -> str:
    return str(inp.get("title") or f"Chapter {chapter_id(inp).removeprefix('ch')}")


def source_md(inp: dict[str, Any]) -> str:
    return str(inp.get("source_md") or inp.get("body_md") or "")


def source_ref(inp: dict[str, Any]) -> str:
    md = source_md(inp)
    match = re.search(r"source_ref:\s*([A-Za-z0-9_.:-]+)", md)
    return match.group(1) if match else "Prob_GZIC-p001"


def source_refs(inp: dict[str, Any]) -> set[str]:
    md = source_md(inp)
    return set(re.findall(r"source_ref:\s*([A-Za-z0-9_.:-]+)", md))


def citation(inp: dict[str, Any]) -> Citation:
    text = source_md(inp).strip().splitlines()
    quote = next((line.strip("# <!->") for line in text if line.strip()), "stub source text")
    return Citation(ref_id=source_ref(inp), quote=quote[:240] or "stub source text")
