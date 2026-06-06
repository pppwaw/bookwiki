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
        body="""你为一本教科书风格的学习网站描述一幅源图像。

返回一个简洁的、基于源的图注和要点。使用附加图像、由标题界定的 `section_context`、
附近源文本、任何 `existing_caption`、图像元数据和 `source_ref`。
不要编造可用上下文中不支持的细节。
保持 `caption_md` 足够短，以便放置在图像下方。""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> VisionCaptionResult:
        image_path = _image_path(inp)
        draft_caption = inp.get("existing_caption") or inp.get("nearby_text")
        draft = VisionCaptionResult(
            caption_md=str(draft_caption or "Source figure."),
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
