from __future__ import annotations

import re

SOURCE_REF_RE = re.compile(r"<!--\s*source_ref:\s*([A-Za-z0-9_.-]+)\s*-->")


def clean_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def source_id_from_stem(stem: str) -> str:
    source_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-")
    return source_id or "source"


BOOK_FIGURE_TAG_RE = re.compile(r"<BookFigure\b[^>]*/>")
_BOOK_FIGURE_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)="([^"]*)"')


def parse_book_figure_tag(tag: str) -> dict[str, str]:
    """Parse a self-closing ``<BookFigure .../>`` tag into its attribute map.

    Attribute values are returned exactly as they appear in the tag (the
    renderer HTML-escapes them via ``html.escape(..., quote=True)``), so callers
    are responsible for ``html.unescape`` on the fields they consume.
    """
    return dict(_BOOK_FIGURE_ATTR_RE.findall(tag))
