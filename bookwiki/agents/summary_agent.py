from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import (
    chapter_document,
    chapter_id,
    chapter_title,
    citation,
    source_refs,
)
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.summary import SummaryResult


class SummaryAgent:
    kind: ClassVar[str] = "summary_llm_v1"
    output_model: ClassVar[type[SummaryResult]] = SummaryResult
    model_key: ClassVar[str] = "summary"
    prompt_name: ClassVar[str] = "summary"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是章节摘要 agent。写出一位优秀的学习搭档会在 30 秒内给出的那种
摘要：生动、具体、即刻可用。

目标：
- summary_md 是对本章所教内容及其重要性的紧凑、面向学习者的概述（2-4 句）。
  先用通俗语言点出核心思想，再补充最重要的“所以呢”。
- 当一个贴切的类比或示例能厘清核心思想时，优先使用它，而非抽象复述。
- key_points 是具体、扎根于源文本的要点（4-8 条），抓住学习者必须记住的内容：
  定义、关键公式（附直觉）、重要区别和常见陷阱。

规则：
- 将 summary_md 写成对核心思想的紧凑解释。
- 将 key_points 写成具体、扎根于源文本的要点，而非泛泛的学习建议。
- key_points 必须是字符串数组。
- 不要在 key_points 中返回对象。
- 只在顶层 citations 数组中放置引用对象。
- 保持引用简短并与源文本绑定。
- 不要引入章节源文本中不存在的概念。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> SummaryResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        refs = source_refs(inp)
        draft = SummaryResult(
            chapter_id=ch_id,
            summary_md=f"{title} introduces the core ideas available in the source bundle.",
            key_points=["Summarize source material", "Preserve citations"],
            citations=[citation(inp)],
            owner_task_id=f"{ch_id}:summary",
        )
        llm_input = _content_input(inp, refs)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SummaryResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=llm_input,
            draft=draft,
            allowed_citation_refs=refs,
        )
        return SummaryResult.model_validate(result)


def _content_input(inp: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    payload = {key: value for key, value in inp.items() if key != "source_md"}
    payload["document_xml"] = chapter_document(inp)
    payload["allowed_source_refs"] = sorted(refs)
    return payload
