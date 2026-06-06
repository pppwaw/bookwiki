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
from bookwiki.schemas.quiz import QuizItem, QuizPlacement, QuizResult


class QuizAgent:
    kind: ClassVar[str] = "quiz_llm_v1"
    output_model: ClassVar[type[QuizResult]] = QuizResult
    model_key: ClassVar[str] = "quiz"
    prompt_name: ClassVar[str] = "quiz"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是测验生成 agent。请设计优秀辅导老师会用来检验真正理解力
（而非模式匹配）的问题。

好问题的特征：
- 每个问题针对章节中的一个具体概念（定义、推导步骤、性质、解释、常见误区）。
- 题干具体且面向学习者。必要时可设定一个小场景
  （"假设你观察到..."，"基于上述估计量..."）。
- 干扰项必须合理：一个有诱惑力的错误选项应反映学习者在快速阅读章节后
  可能产生的真实误解。
- 避免琐碎问题（"有多少节..."）、陷阱措辞，以及需要章节外知识才能回答的问题。

解释：
- 在"答案是 X"之后，用一两句话简要说明*为什么*，并明确指出导致最常见错误
  选项的误解是什么。将其与章节中的具体概念联系起来。

约束条件：
- 创建检验理解力（而非琐碎知识）的选择题。
- 创建适当数量的问题，以 `quiz_per_chapter` 作为上限或目标（当提供时）。
- 每个问题必须至少有两个合理的选项，且恰好有一个答案与其中一个选项匹配。
- 解释应说明答案为什么正确。
- 每个题目使用章节源文本中的引用。

放置（关键——交错分布，不要集中在前端）：
- 不要将所有题目放在章节开头的一个单独放置中。
- 阅读 `chapter_body_blocks`（0 索引的段落列表）。为每个逻辑小节创建一个放置，
  使学习者在阅读完所测试的材料后立即回答相应问题——就像文章中的中途检查点。
- 每个章节目标 2-4 个放置，每个放置包含 1-2 个题目。只有当小节异常长或这些
  题目确实属于彼此相关时，才允许一个放置超过 2 个题目。
- `placements.after_block` 是 `chapter_body_blocks` 中基于 0 的索引，表示
  QuizBlock 插入到该块之后。选择该放置所覆盖的小节结束处的块索引。将这些
  索引分散到整个章节——不要将它们集中在 0 附近。
- `placements.item_indexes` 是基于 1 的 `items` 索引。每个题目必须恰好出现在
  一个放置中；任何题目不得出现两次。
- `placements.title` 是一个简短标题，如 "Checkpoint"、"Quick check"、
  "Practice"，或特定于小节的短语。当有更具描述性的标题适用时，避免使用通用的
  "Quiz"。

避免陷阱问题、模棱两可的措辞以及需要外部知识才能回答的问题。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> QuizResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        refs = source_refs(inp)
        count = _requested_count(inp, "quiz_per_chapter", "quizPerChapter", 1)
        items = [
            QuizItem(
                question=f"What is central idea {index + 1} in {title}?",
                choices=[title, "Unrelated topic"],
                answer=title,
                explanation="The answer should be grounded in the chapter source.",
                citations=[citation(inp)],
            )
            for index in range(count)
        ]
        draft = QuizResult(
            chapter_id=ch_id,
            items=items,
            placements=[
                QuizPlacement(
                    after_block=0,
                    item_indexes=list(range(1, len(items) + 1)),
                    title="Quiz",
                )
            ],
            owner_task_id=f"{ch_id}:quiz",
        )
        llm_input = _content_input(inp, refs)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=QuizResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=llm_input,
            draft=draft,
            allowed_citation_refs=refs,
        )
        return QuizResult.model_validate(result)


def _content_input(inp: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    payload = {key: value for key, value in inp.items() if key != "source_md"}
    payload["document_xml"] = chapter_document(inp)
    payload["allowed_source_refs"] = sorted(refs)
    body = str(inp.get("chapter_body_md", ""))
    if body:
        payload["chapter_body_blocks"] = [
            block.strip() for block in body.split("\n\n") if block.strip()
        ]
    return payload


def _requested_count(inp: dict[str, Any], snake_key: str, camel_key: str, default: int) -> int:
    try:
        value = int(inp.get(snake_key, inp.get(camel_key, default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
