"""Deterministic reducer for the streaming skeleton fold.

``build_skeleton`` no longer ships the whole book to one LLM call. Instead it folds
chapter-by-chapter: each chapter's :class:`~bookwiki.schemas.skeleton.SkeletonFoldResult`
returns *ops* (add/alias/rename/merge/split), and this module applies them to a running
:class:`Registry`. Keeping the reducer in code (not the LLM) makes first-chapter ownership
deterministic, keeps merges auditable, and lets an early wrong ``merge`` be undone by a
later ``split``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from bookwiki.concepts import concept_key as _concept_key
from bookwiki.schemas.skeleton import (
    BookSkeleton,
    CanonicalConcept,
    SkeletonOp,
)
from bookwiki.utils.logging import get_logger

_LATE = 1 << 30  # ordinal for a chapter never seen during the fold
_LOG = get_logger(__name__)


@dataclass
class _Concept:
    canonical: str
    aliases: list[str]
    first_chapter_id: str


@dataclass
class Registry:
    """Running concept table built by folding chapters in order.

    ``apply`` mutates the table for one chapter; ``record_uses`` notes the canonicals a
    chapter referenced but did not introduce; ``to_skeleton`` serialises to the read-only
    :class:`BookSkeleton` contract the rest of the pipeline already consumes.
    """

    _by_key: dict[str, _Concept] = field(default_factory=dict)
    _alias_to_key: dict[str, str] = field(default_factory=dict)
    _chapter_ordinal: dict[str, int] = field(default_factory=dict)
    _uses: dict[str, list[str]] = field(default_factory=dict)
    _variant_owner: dict[str, str] = field(default_factory=dict)

    # -- folding ---------------------------------------------------------------
    def apply(self, ops: Iterable[SkeletonOp], *, current_chapter: str) -> None:
        self._note_chapter(current_chapter)
        for op in ops:
            handler = getattr(self, f"_op_{op.op}", None)
            if handler is None:
                _LOG.warning(
                    "Registry unknown op: op=%s chapter=%s", op.op, current_chapter
                )
                continue
            handler(op, current_chapter)

    def record_uses(self, chapter_id: str, uses: Iterable[str]) -> None:
        resolved: list[str] = []
        for name in uses:
            key = self._resolve(name)
            if key is None:
                continue
            canonical = self._by_key[key].canonical
            if canonical not in resolved:
                resolved.append(canonical)
        self._uses[chapter_id] = resolved

    def uses_for(self, chapter_id: str) -> list[str]:
        return list(self._uses.get(chapter_id, []))

    def compact(self) -> list[dict[str, list[str] | str]]:
        """The registry as a small ``[{canonical, aliases}]`` list for the fold prompt.

        Names only (no source text), so a fold call's input stays tiny regardless of book
        size — this is what keeps the streaming fold from ever overflowing the context.
        """
        return [
            {"canonical": entry.canonical, "aliases": list(entry.aliases)}
            for entry in self._by_key.values()
        ]

    # -- ops -------------------------------------------------------------------
    def _op_add_concept(self, op: SkeletonOp, chapter: str) -> None:
        canonical = (op.canonical or "").strip()
        if not canonical:
            _warn_noop(op, chapter, "empty canonical")
            return
        if not _concept_key(canonical):
            _warn_noop(op, chapter, "canonical has no identity key")
            return
        key = self._resolve(canonical)
        if key is None:
            entry = _Concept(canonical=canonical, aliases=[], first_chapter_id=chapter)
            self._by_key[_concept_key(canonical)] = entry
            self._remember_owner(canonical, chapter)
            for alias in op.aliases:
                self._attach(entry, alias, first_chapter_id=chapter)
            self._reindex()
            return
        # Already known (e.g. a candidate that matched an alias): only enrich aliases,
        # never move ownership — the earliest introducing chapter keeps it.
        entry = self._by_key[key]
        for alias in op.aliases:
            self._attach(entry, alias, first_chapter_id=entry.first_chapter_id)
        self._reindex()

    def _op_add_alias(self, op: SkeletonOp, chapter: str) -> None:
        key = self._resolve(op.canonical or "")
        alias = (op.alias or "").strip()
        if key is None:
            _warn_noop(op, chapter, "canonical does not resolve")
            return
        if not alias:
            _warn_noop(op, chapter, "empty alias")
            return
        self._attach(self._by_key[key], alias, first_chapter_id=self._by_key[key].first_chapter_id)
        self._reindex()

    def _op_rename_canonical(self, op: SkeletonOp, chapter: str) -> None:
        key = self._resolve(op.from_canonical or "")
        new_name = (op.to_canonical or "").strip()
        if key is None:
            _warn_noop(op, chapter, "from_canonical does not resolve")
            return
        if not new_name:
            _warn_noop(op, chapter, "empty to_canonical")
            return
        if not _concept_key(new_name):
            _warn_noop(op, chapter, "to_canonical has no identity key")
            return
        entry = self._by_key.pop(key)
        old = entry.canonical
        entry.canonical = new_name
        new_key = _concept_key(new_name)
        entry.aliases = [a for a in entry.aliases if _concept_key(a) != new_key]
        if _concept_key(old) != new_key and old not in entry.aliases:
            entry.aliases.append(old)
            self._remember_owner(old, entry.first_chapter_id)
        self._by_key[new_key] = entry
        self._reindex()

    def _op_merge(self, op: SkeletonOp, chapter: str) -> None:
        wkey = self._resolve(op.winner or "")
        lkey = self._resolve(op.loser or "")
        if wkey is None:
            _warn_noop(op, chapter, "winner does not resolve")
            return
        if lkey is None:
            _warn_noop(op, chapter, "loser does not resolve")
            return
        if wkey == lkey:
            _warn_noop(op, chapter, "winner and loser resolve to same concept")
            return
        winner = self._by_key[wkey]
        loser = self._by_key.pop(lkey)
        for name in (loser.canonical, *loser.aliases):
            self._attach(winner, name, first_chapter_id=loser.first_chapter_id)
        if self._ordinal(loser.first_chapter_id) < self._ordinal(winner.first_chapter_id):
            winner.first_chapter_id = loser.first_chapter_id
        self._reindex()

    def _op_split(self, op: SkeletonOp, chapter: str) -> None:
        key = self._resolve(op.canonical or "")
        if key is None:
            _warn_noop(op, chapter, "canonical does not resolve")
            return
        if not op.into:
            _warn_noop(op, chapter, "empty split targets")
            return
        original = self._by_key.pop(key)
        for target in op.into:
            name = (target.canonical or "").strip()
            if not name or not _concept_key(name):
                continue
            aliases = [a for a in target.aliases if a]
            first_chapter_id = self._first_owner_for([name, *aliases], original.first_chapter_id)
            entry = _Concept(
                canonical=name,
                aliases=aliases,
                first_chapter_id=first_chapter_id,
            )
            self._by_key[_concept_key(name)] = entry
            self._remember_owner(name, first_chapter_id)
            for alias in aliases:
                self._remember_owner(alias, first_chapter_id)
        self._reindex()

    # -- serialisation ---------------------------------------------------------
    def to_skeleton(
        self, *, chapter_briefs: dict[str, str], chapter_order: list[str]
    ) -> BookSkeleton:
        ordered = sorted(
            self._by_key.values(),
            key=lambda e: (self._ordinal(e.first_chapter_id), _concept_key(e.canonical)),
        )
        glossary = [
            CanonicalConcept(
                canonical=e.canonical,
                aliases=list(e.aliases),
                first_chapter_id=e.first_chapter_id,
            )
            for e in ordered
        ]
        return BookSkeleton(
            glossary=glossary,
            alias_map=_build_alias_map(glossary),
            chapter_briefs=dict(chapter_briefs),
            chapter_order=list(chapter_order),
            chapter_uses={ch_id: list(uses) for ch_id, uses in self._uses.items() if uses},
        )

    # -- internals -------------------------------------------------------------
    def _note_chapter(self, chapter_id: str) -> None:
        if chapter_id and chapter_id not in self._chapter_ordinal:
            self._chapter_ordinal[chapter_id] = len(self._chapter_ordinal)

    def _ordinal(self, chapter_id: str) -> int:
        return self._chapter_ordinal.get(chapter_id, _LATE)

    def _resolve(self, name: str) -> str | None:
        key = _concept_key(name or "")
        if not key:
            return None
        if key in self._by_key:
            return key
        return self._alias_to_key.get(key)

    def _attach(
        self, entry: _Concept, alias: str, *, first_chapter_id: str | None = None
    ) -> None:
        alias = (alias or "").strip()
        if not alias or alias == entry.canonical:
            return
        if _concept_key(alias) == _concept_key(entry.canonical):
            return
        if alias not in entry.aliases:
            entry.aliases.append(alias)
        self._remember_owner(alias, first_chapter_id or entry.first_chapter_id)

    def _remember_owner(self, variant: str, first_chapter_id: str) -> None:
        key = _concept_key(variant or "")
        if key and first_chapter_id:
            self._variant_owner[key] = first_chapter_id

    def _first_owner_for(self, variants: Iterable[str], fallback: str) -> str:
        recorded_owners = [
            owner
            for variant in variants
            if (owner := self._variant_owner.get(_concept_key(variant or "")))
        ]
        if not recorded_owners:
            return fallback
        return min(recorded_owners, key=self._ordinal)

    def _reindex(self) -> None:
        self._alias_to_key = {}
        for key, entry in self._by_key.items():
            for variant in (entry.canonical, *entry.aliases):
                vkey = _concept_key(variant)
                if vkey:
                    self._alias_to_key[vkey] = key


def _build_alias_map(glossary: list[CanonicalConcept]) -> dict[str, str]:
    """variant (raw + normalised) → canonical, matching ``SkeletonAgent`` output."""
    alias_map: dict[str, str] = {}
    for entry in glossary:
        for variant in (entry.canonical, *entry.aliases):
            alias_map[variant] = entry.canonical
            normalized = _concept_key(variant)
            if normalized:
                alias_map[normalized] = entry.canonical
    return alias_map


def _warn_noop(op: SkeletonOp, current_chapter: str, reason: str) -> None:
    _LOG.warning(
        "Registry op no-op: op=%s chapter=%s reason=%s", op.op, current_chapter, reason
    )
