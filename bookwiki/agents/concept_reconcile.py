from __future__ import annotations

import re
from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
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
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        version="v1",
        body="""You are the concept-reconciliation agent.

Merge concept candidates that refer to the same idea.
Choose stable canonical names that are concise and pedagogically useful.
Keep source_chapter_ids complete and deduplicated.
Populate alias_map so every alias and every original candidate name maps to its
canonical concept.
Do not merge concepts that are merely related but distinct.""",
    )

    async def run(
        self, inp: list[dict[str, Any]], *, model: str, runtime: LLMRuntime
    ) -> ConceptReconcileResult:
        by_key: dict[str, ConceptReconciledItem] = {}
        alias_to_key: dict[str, str] = {}
        for item in inp:
            canonical = str(item["name"])
            aliases = [str(alias) for alias in item.get("aliases", [])]
            chapter_id = str(item.get("source_chapter_id", "ch01"))
            names = [canonical, *aliases]
            matched_key = next(
                (
                    alias_to_key[_concept_key(name)]
                    for name in names
                    if _concept_key(name) in alias_to_key
                ),
                None,
            )
            key = matched_key or _concept_key(canonical)
            existing = by_key.get(key)
            if existing is None:
                existing = ConceptReconciledItem(
                    canonical=canonical,
                    aliases=[],
                    source_chapter_ids=[chapter_id],
                )
                by_key[key] = existing
            elif chapter_id not in existing.source_chapter_ids:
                existing.source_chapter_ids.append(chapter_id)
            for name in names:
                normalized = _concept_key(name)
                alias_to_key[normalized] = key
                if name != existing.canonical and name not in existing.aliases:
                    existing.aliases.append(name)

        alias_map: dict[str, str] = {}
        for item in by_key.values():
            alias_map[item.canonical] = item.canonical
            alias_map[_concept_key(item.canonical)] = item.canonical
            for alias in item.aliases:
                alias_map[alias] = item.canonical
                alias_map[_concept_key(alias)] = item.canonical
        draft = ConceptReconcileResult(concepts=list(by_key.values()), alias_map=alias_map)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptReconcileResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return ConceptReconcileResult.model_validate(result)


def _concept_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)
