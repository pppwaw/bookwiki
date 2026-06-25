from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from bookwiki.schemas.common import VersionedModel
from bookwiki.schemas.source import ConceptCandidate


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
    # canonical concepts each chapter references but does not own (from the fold's
    # ``uses``). Lets ``_skeleton_payload`` ship each section only the terms its chapter
    # actually touches, instead of the whole-book glossary.
    chapter_uses: dict[str, list[str]] = Field(default_factory=dict)


class SplitTarget(VersionedModel):
    """One concept a ``split`` op breaks an over-merged entry back into."""

    canonical: str
    aliases: list[str] = Field(default_factory=list)


class SkeletonOp(VersionedModel):
    """A single edit to the running concept registry, emitted by ``SkeletonFoldAgent``.

    The agent never returns the whole table; it returns ops that a deterministic reducer
    (:func:`bookwiki.skeleton.fold.Registry.apply`) applies. Keeping ops + a code-side
    reducer makes ownership (first chapter) deterministic and keeps the merge decisions
    auditable. ``split`` is mandatory so an early wrong ``merge`` can be undone later.
    """

    op: Literal["add_concept", "add_alias", "rename_canonical", "merge", "split"]
    # add_concept / add_alias target
    canonical: str | None = None
    aliases: list[str] = Field(default_factory=list)
    alias: str | None = None
    # rename_canonical
    from_canonical: str | None = None
    to_canonical: str | None = None
    # merge (loser folds into winner as an alias)
    winner: str | None = None
    loser: str | None = None
    # split (canonical breaks into several)
    into: list[SplitTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_op_fields(self) -> SkeletonOp:
        required: dict[str, tuple[str, ...]] = {
            "add_concept": ("canonical",),
            "add_alias": ("canonical", "alias"),
            "rename_canonical": ("from_canonical", "to_canonical"),
            "merge": ("winner", "loser"),
            "split": ("canonical", "into"),
        }
        disallowed: dict[str, tuple[str, ...]] = {
            "add_concept": (
                "alias",
                "from_canonical",
                "to_canonical",
                "winner",
                "loser",
                "into",
            ),
            "add_alias": (
                "aliases",
                "from_canonical",
                "to_canonical",
                "winner",
                "loser",
                "into",
            ),
            "rename_canonical": (
                "canonical",
                "aliases",
                "alias",
                "winner",
                "loser",
                "into",
            ),
            "merge": (
                "canonical",
                "aliases",
                "alias",
                "from_canonical",
                "to_canonical",
                "into",
            ),
            "split": (
                "aliases",
                "alias",
                "from_canonical",
                "to_canonical",
                "winner",
                "loser",
            ),
        }

        op = self.op
        for field in required[op]:
            if not _field_non_empty(getattr(self, field)):
                msg = f"SkeletonOp op={op!r} requires {field!r} to be non-empty"
                raise ValueError(msg)
        for field in disallowed[op]:
            if _field_non_empty(getattr(self, field)):
                msg = f"SkeletonOp op={op!r} must not set {field!r}"
                raise ValueError(msg)
        return self


def _field_non_empty(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    return bool(str(value or "").strip())


class SkeletonFoldResult(VersionedModel):
    """One chapter's contribution to the streaming skeleton fold.

    ``ops`` mutate the shared registry; ``uses`` names the already-registered canonicals
    this chapter references but did not introduce (feeds ``chapter_uses`` for per-section
    term slicing).
    """

    ops: list[SkeletonOp] = Field(default_factory=list)
    uses: list[str] = Field(default_factory=list)


class SkeletonExtractResult(VersionedModel):
    """Concept candidates pulled from one chapter source chunk (pass 1 of build_skeleton).

    Recall-oriented: just names + the refs they appear in. Cross-language/synonym merging
    is deferred to the serial fold, which sees the whole running registry in context.
    """

    candidates: list[ConceptCandidate] = Field(default_factory=list)
