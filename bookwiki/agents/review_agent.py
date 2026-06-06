from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import RepairResult


class ReviewAgent:
    kind: ClassVar[str] = "review_llm_v1"
    output_model: ClassVar[type[RepairResult]] = RepairResult
    model_key: ClassVar[str] = "review"
    prompt_name: ClassVar[str] = "review"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是修复审查 agent。

给定一个所有者任务和问题上下文，提出一个聚焦的修复措施。
该措施应足够具体，以便调度器或人工执行。
备注应说明疑似根因和最小的纠正步骤。
除非输入证明内容已修复，否则不要声称内容已修复。""",
    )

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
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return RepairResult.model_validate(result)
