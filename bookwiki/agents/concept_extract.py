from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.concept import ConceptCandidate, ConceptExtractResult


class ConceptExtractAgent:
    kind: ClassVar[str] = "concept_extract_llm_v1"
    output_model: ClassVar[type[ConceptExtractResult]] = ConceptExtractResult
    model_key: ClassVar[str] = "concept"
    prompt_name: ClassVar[str] = "concept_extract"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是概念提取 agent。

在章节源文本中识别最重要的规范概念。
使用适合 Fumadocs 概念页面的简洁名称。
别名应包含源文本中出现的常见变体、缩写或替代拼写。
所选概念必须是章节的核心，而非偶然出现的示例。""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> ConceptExtractResult:
        ch_id = chapter_id(inp)
        concepts = inp.get("concepts", [])
        return ConceptExtractResult(
            concepts=[
                ConceptCandidate(
                    name=str(name),
                    aliases=[],
                    source_chapter_id=ch_id,
                    owner_task_id=f"{ch_id}:concept_extract",
                )
                for name in concepts
                if str(name).strip()
            ]
        )
