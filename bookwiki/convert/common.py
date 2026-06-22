from __future__ import annotations

import re
import unicodedata
from hashlib import sha256

SOURCE_REF_RE = re.compile(r"<!--\s*source_ref:\s*([A-Za-z0-9_.-]+)\s*-->")

# Names Windows refuses as a path segment (case-insensitive, with or without an extension).
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_MAX_SLUG_LEN = 80


def slugify_path_segment(value: str, *, fallback_prefix: str = "item") -> str:
    """Turn a free-form (often CJK) name into one safe filesystem / URL path segment.

    The result is used verbatim as a directory name, a ``.mdx`` filename stem, and a site URL slug,
    so it must be stable, deterministic, and free of separators/traversal. CJK characters are
    preserved (the fumadocs site already routes non-ASCII slugs); only the hardening differs from a
    naive slug:

    - Unicode is NFC-normalised so visually identical names map to one slug.
    - Any run of characters outside ``[\\w.-]`` (spaces, ``/``, ``:``, fullwidth ``：`` …) collapses
      to a single ``-``; this guarantees no ``:`` survives, so ``owner_task_id`` ``<id>:<kind>``
      parsing via ``partition(':')`` stays sound.
    - Leading/trailing ``.``/``-`` are stripped, so a segment can never be ``.``/``..`` or hidden.
    - Over-long names are truncated with a stable hash tail to stay unique-ish and within FS limits.
    - Empty results and Windows reserved device names fall back to ``<prefix>-<hash8>``.

    Collision handling across multiple names is the caller's job; this function maps one name to one
    segment deterministically.
    """
    text = unicodedata.normalize("NFC", str(value)).strip()
    text = re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    if len(text) > _MAX_SLUG_LEN:
        digest = sha256(str(value).encode("utf-8")).hexdigest()[:8]
        text = text[: _MAX_SLUG_LEN - 9].strip("-.") + "-" + digest
    if not text or text.casefold() in _WINDOWS_RESERVED_NAMES:
        digest = sha256(str(value).encode("utf-8")).hexdigest()[:8]
        text = f"{fallback_prefix}-{digest}"
    return text


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
