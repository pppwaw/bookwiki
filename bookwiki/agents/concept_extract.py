from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.concept import ConceptCandidate, ConceptExtractResult


class ConceptExtractAgent:
    kind: ClassVar[str] = "concept_extract_llm_v1"
    output_model: ClassVar[type[ConceptExtractResult]] = ConceptExtractResult
    model_key: ClassVar[str] = "concept"
    prompt_name: ClassVar[str] = "concept_extract"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        version="v1",
        body="""You are the concept-extraction agent.

Identify the most important canonical concept in the chapter source.
Use a concise name suitable for a Fumadocs concept page.
Aliases should include common variants, abbreviations, or alternate spellings present
in the source.
The selected concept must be central to the chapter, not an incidental example.""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> ConceptExtractResult:
        ch_id = chapter_id(inp)
        concepts = inp.get("concepts", [])
        return ConceptExtractResult(
            concepts=[
                ConceptCandidate(
                    name=str(name),
                    aliases=[],
                    source_chapter_id=ch_id,
                    owner_task_id=f"{ch_id}:concept_extract",
                )
                for name in concepts
                if str(name).strip()
            ]
        )
