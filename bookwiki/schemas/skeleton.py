from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import VersionedModel


class CanonicalConcept(VersionedModel):
    """A book-wide canonical concept entry produced by ``SkeletonAgent``.

    Each entry collapses one or more variant names (``aliases``) into a single
    ``canonical`` string and records the chapter where the concept is first
    introduced (``first_chapter_id``). Downstream chapters cite the concept by
    its canonical name and may not redefine it; only the first chapter "owns"
    the definition.
    """

    canonical: str
    aliases: list[str] = Field(default_factory=list)
    first_chapter_id: str


class BookSkeleton(VersionedModel):
    """Pre-generation contract that keeps independently-generated chapters aligned.

    The skeleton is produced before ``generate`` runs and is treated as
    read-only by every downstream stage. It carries:

    - ``glossary``: every concept the book will discuss, with a single canonical
      name and an explicit first-occurrence chapter (so later chapters reference
      rather than redefine).
    - ``alias_map``: any variant name (raw or normalised) → canonical, used by
      ``SectionAgent`` and the integrator to converge term drift.
    - ``chapter_briefs``: a one-line summary per chapter so neighbouring
      chapters can write transitions ("the previous chapter introduced X; here
      we build on Y") without seeing each other's full body.
    - ``chapter_order``: ``chapter_id`` order for neighbour lookup.
    """

    glossary: list[CanonicalConcept] = Field(default_factory=list)
    alias_map: dict[str, str] = Field(default_factory=dict)
    chapter_briefs: dict[str, str] = Field(default_factory=dict)
    chapter_order: list[str] = Field(default_factory=list)
