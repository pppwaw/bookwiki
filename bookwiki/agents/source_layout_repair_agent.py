from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import SourceLayoutRepairResult


class SourceLayoutRepairAgent:
    kind: ClassVar[str] = "source_layout_repair_llm_v1"
    output_model: ClassVar[type[SourceLayoutRepairResult]] = SourceLayoutRepairResult
    model_key: ClassVar[str] = "source_layout_repair"
    prompt_name: ClassVar[str] = "source_layout_repair"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是源布局修复 agent。

你收到来自 MinerU content_list 输出的低置信度布局候选。
仅返回保留源文本和物理页面归属的结构化补丁。
允许的操作：
- link_table_parts：连接属于同一逻辑表格的相邻表格/图表块。
- attach_caption：将标注块附加到图像/表格/图表块上。
- promote_heading：将类标题块标记为标题。
- demote_repeating_header_footer：将重复的页面噪音标记为页眉/页脚。

绝不改写源内容。绝不编造 block ID。仅使用输入候选和上下文中出现的 block ID。""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> SourceLayoutRepairResult:
        candidates = inp.get("candidates") if isinstance(inp.get("candidates"), list) else []
        draft = SourceLayoutRepairResult(
            patches=[],
            notes=(
                "No structural repair needed."
                if not candidates
                else "Review candidates and return only high-confidence structural patches."
            ),
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SourceLayoutRepairResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return SourceLayoutRepairResult.model_validate(result)
