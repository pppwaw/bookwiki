from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.concept import (
    ConceptReconciledItem,
    ConceptReconcileResult,
)


class ConceptReconcileAgent:
    kind: ClassVar[str] = "concept_reconcile"
    output_model: ClassVar[type[ConceptReconcileResult]] = ConceptReconcileResult
    model_key: ClassVar[str] = "concept"

    async def run(self, inp: list[dict[str, Any]], *, model: str) -> ConceptReconcileResult:
        by_name: dict[str, ConceptReconciledItem] = {}
        alias_map: dict[str, str] = {}
        for item in inp:
            canonical = str(item["name"])
            aliases = [str(alias) for alias in item.get("aliases", [])]
            chapter_id = str(item.get("source_chapter_id", "ch01"))
            existing = by_name.get(canonical)
            if existing is None:
                existing = ConceptReconciledItem(
                    canonical=canonical,
                    aliases=aliases,
                    source_chapter_ids=[chapter_id],
                )
                by_name[canonical] = existing
            elif chapter_id not in existing.source_chapter_ids:
                existing.source_chapter_ids.append(chapter_id)
            alias_map[canonical] = canonical
            for alias in aliases:
                alias_map[alias] = canonical
        return ConceptReconcileResult(concepts=list(by_name.values()), alias_map=alias_map)
