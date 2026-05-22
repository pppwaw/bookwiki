from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import RepairResult


class ReviewAgent:
    kind: ClassVar[str] = "review_llm_v1"
    output_model: ClassVar[type[RepairResult]] = RepairResult
    model_key: ClassVar[str] = "review"
    prompt_name: ClassVar[str] = "review"

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> RepairResult:
        owner = str(inp.get("owner_task_id", "unknown:review"))
        draft = RepairResult(
            owner_task_id=owner,
            action="review",
            notes="Review the issue and propose a targeted repair.",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=RepairResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            inp=inp,
            draft=draft,
        )
        return RepairResult.model_validate(result)
