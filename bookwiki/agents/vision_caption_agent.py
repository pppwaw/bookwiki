from __future__ import annotations

from pathlib import Path
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

Return a concise source-grounded caption and key points. Use the attached image,
the heading-bounded section_context, nearby source text, image metadata, and
source_ref. Do not invent details not supported by the available context. Keep
caption_md short enough to place below the image.""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> VisionCaptionResult:
        image_path = _image_path(inp)
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
            inp=_prompt_input(inp),
            draft=draft,
            image_paths=[image_path],
        )
        return VisionCaptionResult.model_validate(result)


def _image_path(inp: dict[str, Any]) -> Path:
    raw = inp.get("asset_full_path") or inp.get("asset_path")
    if not isinstance(raw, str) or not raw.strip():
        raise FileNotFoundError("vision caption requires asset_full_path or asset_path")
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"vision caption image not found: {path}")
    return path


def _prompt_input(inp: dict[str, Any]) -> dict[str, Any]:
    hidden_keys = {"asset_full_path", "asset_sha256"}
    return {key: value for key, value in inp.items() if key not in hidden_keys}
