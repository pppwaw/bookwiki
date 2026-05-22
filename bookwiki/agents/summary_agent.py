from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation, source_refs
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.summary import SummaryResult


class SummaryAgent:
    kind: ClassVar[str] = "summary_llm_v1"
    output_model: ClassVar[type[SummaryResult]] = SummaryResult
    model_key: ClassVar[str] = "summary"
    prompt_name: ClassVar[str] = "summary"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        version="v2",
        body="""You are the chapter-summary agent.

Summarize the chapter for fast review.
Write summary_md as a compact explanation of the core ideas.
Write key_points as specific, source-grounded bullets, not generic study advice.
key_points must be an array of strings.
Do not return objects inside key_points.
Put citation objects only in the top-level citations array.
Keep citations short and tied to the source text.
Do not introduce concepts that are absent from the chapter source.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> SummaryResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        draft = SummaryResult(
            chapter_id=ch_id,
            summary_md=f"{title} introduces the core ideas available in the source bundle.",
            key_points=["Summarize source material", "Preserve citations"],
            citations=[citation(inp)],
            owner_task_id=f"{ch_id}:summary",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SummaryResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
            allowed_citation_refs=source_refs(inp),
        )
        return SummaryResult.model_validate(result)
