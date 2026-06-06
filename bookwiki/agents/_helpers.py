from __future__ import annotations

import re
from html import escape, unescape
from typing import Any

from bookwiki.convert.common import BOOK_FIGURE_TAG_RE, parse_book_figure_tag
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


def _placeholder_figures(text: str) -> str:
    """Replace raw ``<BookFigure .../>`` tags with readable text placeholders.

    The chapter document is HTML-escaped before being handed to the LLM, which
    would turn figure tags into ``&lt;BookFigure&gt;`` noise. Converting them to
    ``[Figure <id>: <caption>]`` first keeps the figure visible and prompts the
    model to reference it by id in its draft.
    """

    def _replace(match: re.Match[str]) -> str:
        attrs = parse_book_figure_tag(match.group(0))
        figure_id = unescape(attrs.get("id", "")).strip()
        if not figure_id:
            return match.group(0)
        caption = unescape(attrs.get("caption", "")).strip()
        if caption:
            return f"[Figure {figure_id}: {caption}]"
        return f"[Figure {figure_id}]"

    return BOOK_FIGURE_TAG_RE.sub(_replace, text)


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
        body = _placeholder_figures(body)
        chunks.append(
            f'  <chunk ref="{escape(ref_id, quote=True)}">'
            f"{escape(body)}"
            "</chunk>"
        )
    return "<document>\n" + "\n".join(chunks) + "\n</document>"
