from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.concept import (
    ConceptReconciledItem,
    ConceptReconcileResult,
)


class ConceptReconcileAgent:
    kind: ClassVar[str] = "concept_reconcile_llm_v1"
    output_model: ClassVar[type[ConceptReconcileResult]] = ConceptReconcileResult
    model_key: ClassVar[str] = "concept"
    prompt_name: ClassVar[str] = "concept_reconcile"

    async def run(
        self, inp: list[dict[str, Any]], *, model: str, runtime: LLMRuntime
    ) -> ConceptReconcileResult:
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
        draft = ConceptReconcileResult(concepts=list(by_name.values()), alias_map=alias_map)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptReconcileResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            inp=inp,
            draft=draft,
        )
        return ConceptReconcileResult.model_validate(result)
