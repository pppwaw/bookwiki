from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title
from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.concept import ConceptCandidate


class ConceptExtractAgent:
    kind: ClassVar[str] = "concept_extract_llm_v1"
    output_model: ClassVar[type[ConceptCandidate]] = ConceptCandidate
    model_key: ClassVar[str] = "concept"

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> ConceptCandidate:
        ch_id = chapter_id(inp)
        name = f"{chapter_title(inp)} concept"
        draft = ConceptCandidate(
            name=name,
            aliases=[name.lower()],
            source_chapter_id=ch_id,
            owner_task_id=f"{ch_id}:concept_extract",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptCandidate,
            agent_name=self.__class__.__name__,
            task="Extract the most important canonical concept from the chapter source.",
            inp=inp,
            draft=draft,
        )
        return ConceptCandidate.model_validate(result)
