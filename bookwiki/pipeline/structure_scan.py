"""Deterministic source-ref scanning + coverage audit for the structure stage.

The structure stage summarises a >1M-token book by chunking it and summarising each
chunk with an LLM. No single LLM call sees the whole book, so chapter *detection* is the
LLM's job, but **coverage** — the guarantee that not one ``<!-- source_ref -->`` was
silently dropped at a chunk seam or by a truncated call — is enforced here, deterministically.

``scan_source_refs`` reads the full text with a regex (never an LLM) to get the complete
ref set; ``audit_coverage`` diffs it against the refs the produced structure actually
covers. A non-empty result MUST raise upstream rather than silently shipping half a book.
"""

from __future__ import annotations

from collections.abc import Iterable

from bookwiki.convert.common import SOURCE_REF_RE


def scan_source_refs(text: str) -> set[str]:
    """Every ``<!-- source_ref:ID -->`` id present in ``text`` (full, un-chunked)."""
    return set(SOURCE_REF_RE.findall(text))


def audit_coverage(all_refs: Iterable[str], covered_refs: Iterable[str]) -> list[str]:
    """Source refs present in the source but missing from the produced structure.

    Returns a sorted list of dropped refs; an empty list means full coverage. Callers
    raise on a non-empty result — a missing ref means the structure silently lost part of
    the book (the exact failure this stage exists to prevent).
    """
    return sorted(set(all_refs) - set(covered_refs))
