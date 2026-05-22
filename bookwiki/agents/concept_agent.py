from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptAgent:
    kind: ClassVar[str] = "concept_llm_v1"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "concept"

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
            task="Write an Obsidian-ready concept page grounded in linked chapter context.",
            inp=inp,
            draft=draft,
        )
        return ConceptResult.model_validate(result)
