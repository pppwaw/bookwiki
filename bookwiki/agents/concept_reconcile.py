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
        body="""你是概念协调 agent。

合并指向同一概念的概念候选项。
选择简洁且具有教学价值的稳定规范名称。
保持 `source_chapter_ids` 完整并去重。
填充 `alias_map`，使每个别名和每个原始候选名称都映射到其规范概念。
不要合并仅相关但不相同的概念。""",
    )

    async def run(
        self, inp: list[dict[str, Any]] | dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> ConceptReconcileResult:
        candidates = inp.get("candidates", []) if isinstance(inp, dict) else inp
        by_key: dict[str, ConceptReconciledItem] = {}
        alias_to_key: dict[str, str] = {}
        for item in candidates:
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
