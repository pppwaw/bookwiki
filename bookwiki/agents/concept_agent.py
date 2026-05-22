from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptAgent:
    kind: ClassVar[str] = "concept_llm_v1"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "concept"
    prompt_name: ClassVar[str] = "concept"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        version="v1",
        body="""You are the concept-page agent.

Write a concise concept page suitable for an Obsidian vault.
Explain the concept, why it matters, and how it relates to linked chapters.
Use related only for closely connected concepts that are supported by input.
Keep citations grounded in available chapter/source context.
Do not invent cross-links or facts.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ConceptResult:
        name = str(inp.get("canonical") or inp.get("name") or "Concept")
        chapters = [str(ch) for ch in inp.get("source_chapter_ids", ["ch01"])]
        draft = ConceptResult(
            name=name,
            body_md=f"{name} is a reconciled concept linked from {', '.join(chapters)}.",
            related=[],
            citations=[Citation(ref_id=f"{chapters[0]}-concept", quote="concept source")],
            owner_task_id=f"concept:{name}",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return ConceptResult.model_validate(result)
