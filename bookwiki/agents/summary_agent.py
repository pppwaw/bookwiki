from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.summary import SummaryResult


class SummaryAgent:
    kind: ClassVar[str] = "summary_llm_v1"
    output_model: ClassVar[type[SummaryResult]] = SummaryResult
    model_key: ClassVar[str] = "summary"
    prompt_name: ClassVar[str] = "summary"

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
            inp=inp,
            draft=draft,
        )
        return SummaryResult.model_validate(result)
