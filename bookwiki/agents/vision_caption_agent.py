from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import (
    VisionCaptionBatchResult,
    VisionCaptionItem,
)


class VisionCaptionAgent:
    kind: ClassVar[str] = "vision_caption_llm_v2"
    output_model: ClassVar[type[VisionCaptionBatchResult]] = VisionCaptionBatchResult
    model_key: ClassVar[str] = "vision"
    prompt_name: ClassVar[str] = "vision_caption"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你为一本教科书风格的学习网站描述一组源图像。

输入里的 `images` 数组顺序与附加图像顺序完全一致：第 1 个 `images` 元素对应第 1 张附加图，
第 2 个元素对应第 2 张附加图，依此类推。每个元素都有稳定的 `block_id`、`source_ref`、
`section_context`、`nearby_text`、`existing_caption`、`bbox` 等源信息。

请按下面规则输出：

1. 必须为每个输入图像返回且只返回一条 `captions`；数量必须与 `images` 数量一致。
2. 每条 `captions[*].block_id` 和 `captions[*].source_ref` 必须逐字复制对应输入图像的值。
3. 不要把不同图像的内容、标签、变量或结论混在一起；先确认当前 caption 对应哪一张附加图。
4. `caption_md` 写成可直接放在图下的一句话，优先说明图中关系、结构、变量或实验现象；
   不要写“这是一张图片/图中显示”等空泛描述。
5. 若某张图有 `existing_caption`，请修正和补全它；若它明显不完整或有误，以图像和上下文为准。
6. `key_points` 只列该图真正支持的 1–4 个要点，避免重复 `caption_md`。
7. 不要编造图像、附近文本或章节上下文中没有依据的数值、标签、结论。
8. 保持每条 `caption_md` 简洁，通常 20–60 个中文字符或等量英文；必要的数学符号可保留。""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> VisionCaptionBatchResult:
        images = _caption_images(inp)
        image_paths = [_image_path(item) for item in images]
        draft = VisionCaptionBatchResult(
            captions=[
                VisionCaptionItem(
                    block_id=str(item.get("block_id") or ""),
                    caption_md=str(
                        item.get("existing_caption") or item.get("nearby_text") or "Source figure."
                    ),
                    key_points=[],
                    source_ref=str(item.get("source_ref") or inp.get("source_ref") or ""),
                    confidence=0.0,
                )
                for item in images
            ]
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=VisionCaptionBatchResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=_batch_prompt_input(inp),
            draft=draft,
            image_paths=image_paths,
        )
        return VisionCaptionBatchResult.model_validate(result)


def _caption_images(inp: dict[str, Any]) -> list[dict[str, Any]]:
    images = [item for item in inp.get("images") or [] if isinstance(item, dict)]
    if images:
        return images
    return [inp]


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


def _batch_prompt_input(inp: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in inp.items() if key != "images"}
    payload["images"] = [_prompt_input(item) for item in _caption_images(inp)]
    return payload
