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
        body="""You are the source-layout repair agent.

You receive low-confidence layout candidates from MinerU content_list output.
Return only structural patches that preserve source text and physical page ownership.
Allowed actions:
- link_table_parts: connect adjacent table/chart blocks that are one logical table.
- attach_caption: attach a caption block to an image/table/chart block.
- promote_heading: mark a title-like block as a heading.
- demote_repeating_header_footer: mark repeated page noise as header/footer.

Never rewrite source content. Never invent block IDs. Use only block IDs shown in the
input candidates and context.""",
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
