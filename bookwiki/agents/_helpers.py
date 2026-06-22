from __future__ import annotations

import re
from collections.abc import Iterable
from html import escape, unescape
from typing import Any

from bookwiki.convert.common import BOOK_FIGURE_TAG_RE, parse_book_figure_tag
from bookwiki.schemas.common import Citation

SOURCE_REF_RE = re.compile(r"<!--\s*source_ref:\s*([A-Za-z0-9_.:-]+)\s*-->")


def chapter_id(inp: dict[str, Any]) -> str:
    value = str(inp.get("chapter_id") or inp.get("chapter") or "").strip()
    if not value:
        msg = "agent input is missing required 'chapter_id'"
        raise ValueError(msg)
    return value


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


def body_figure_refs(text: str) -> list[str]:
    """Return the de-duplicated, in-order ``<BookFigure>`` ids present in ``text``.

    Quiz agents use this as the allow-list of figures a question may reference: a
    quiz can only point at a figure that actually appears in the body it is built
    from, never an invented id.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for tag in BOOK_FIGURE_TAG_RE.findall(text):
        figure_id = unescape(parse_book_figure_tag(tag).get("id", "")).strip()
        if figure_id and figure_id not in seen:
            seen.add(figure_id)
            refs.append(figure_id)
    return refs


def prune_figure_refs(items: Iterable[Any], allowed: Iterable[str]) -> None:
    """Clear any quiz item ``figure_ref`` not present in the allowed figure id set.

    A quiz may only reference a figure that actually appears in the body it was built
    from; a hallucinated id would otherwise resolve to nothing downstream, so we drop it
    here and let the question stand on its own text.
    """
    allow = {str(ref).strip() for ref in allowed if str(ref).strip()}
    for item in items:
        ref = str(getattr(item, "figure_ref", "") or "").strip()
        if ref and ref not in allow:
            item.figure_ref = ""


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
