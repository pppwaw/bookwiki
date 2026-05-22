from __future__ import annotations

import re
from html import escape
from typing import Any

from bookwiki.schemas.common import Citation

SOURCE_REF_RE = re.compile(r"<!--\s*source_ref:\s*([A-Za-z0-9_.:-]+)\s*-->")


def chapter_id(inp: dict[str, Any]) -> str:
    return str(inp.get("chapter_id") or inp.get("chapter") or "ch01")


def chapter_title(inp: dict[str, Any]) -> str:
    return str(inp.get("title") or f"Chapter {chapter_id(inp).removeprefix('ch')}")


def source_md(inp: dict[str, Any]) -> str:
    return str(inp.get("source_md") or inp.get("body_md") or "")


def source_ref(inp: dict[str, Any]) -> str:
    refs = source_refs(inp)
    if not refs:
        msg = "chapter source contains no source_ref comments"
        raise ValueError(msg)
    return sorted(refs)[0]


def source_refs(inp: dict[str, Any]) -> set[str]:
    md = source_md(inp)
    return set(SOURCE_REF_RE.findall(md))


def citation(inp: dict[str, Any]) -> Citation:
    text = source_md(inp).strip().splitlines()
    quote = next((line.strip("# <!->") for line in text if line.strip()), "stub source text")
    return Citation(ref_id=source_ref(inp), quote=quote[:240] or "stub source text")


def chapter_document(inp: dict[str, Any]) -> str:
    md = source_md(inp)
    matches = list(SOURCE_REF_RE.finditer(md))
    if not matches:
        msg = "chapter source contains no source_ref comments"
        raise ValueError(msg)

    prefix = md[: matches[0].start()].strip()
    chunks: list[str] = []
    for index, match in enumerate(matches):
        ref_id = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(md)
        body = md[start:end].strip()
        if index == 0 and prefix:
            body = f"{prefix}\n\n{body}".strip()
        chunks.append(
            f'  <chunk ref="{escape(ref_id, quote=True)}">'
            f"{escape(body)}"
            "</chunk>"
        )
    return "<document>\n" + "\n".join(chunks) + "\n</document>"
