from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import VisionCaptionResult


class VisionCaptionAgent:
    kind: ClassVar[str] = "vision_caption_llm_v1"
    output_model: ClassVar[type[VisionCaptionResult]] = VisionCaptionResult
    model_key: ClassVar[str] = "vision"
    prompt_name: ClassVar[str] = "vision_caption"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""You describe one source image for a textbook-style learning site.

Return a concise source-grounded caption and key points. Use the image metadata,
nearby source text, and source_ref. Do not invent details not supported by the
available context. Keep caption_md short enough to place below the image.""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> VisionCaptionResult:
        draft = VisionCaptionResult(
            caption_md=str(inp.get("nearby_text") or "Source figure."),
            key_points=[],
            source_ref=str(inp.get("source_ref") or ""),
            confidence=0.0,
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=VisionCaptionResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return VisionCaptionResult.model_validate(result)
