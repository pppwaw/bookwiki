from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.section import SectionPlan, SectionSpec


class SectionPlannerAgent:
    """Split a chapter into teaching units (sections) before section generation.

    Mirrors :class:`SkeletonAgent`: a deterministic draft is built locally
    (one section per curated topic, with a floor of one section so topic-less
    chapters still produce output), giving the LLM a structured starting point
    and giving ``TestLLMRuntime`` an offline result to echo. The LLM then
    regroups topics into coherent teaching units - it may bind several related
    topics into one section or add a short bridging section, but the number of
    sections never exceeds the number of topics.
    """

    kind: ClassVar[str] = "section_planner_llm_v1"
    output_model: ClassVar[type[SectionPlan]] = SectionPlan
    model_key: ClassVar[str] = "section_planner"
    prompt_name: ClassVar[str] = "section_planner"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 BookWiki 的切段（section planner）agent。在章节正文生成之前，
你的任务是把本章拆分为若干**教学单元**（section），供后续逐段生成。

输入提供：本章源文本 `source_md`、已批准结构里的 `topics`、全书骨架子集
（`chapter_owns` 本章拥有的概念、`chapter_uses` 仅引用他章的概念、`prev_brief`/
`next_brief` 邻章一句话摘要）。

切段原则：
- `topics` 是「本章必须讲授的知识点」，但不是教学单元。请按教学逻辑把相关的
  topic 绑定到同一段，或在必要时加入一个简短的铺垫段。
- **段数不得超过 topics 数量**（topics 为空时只产一段）。宁可少切，不要把一个
  topic 拆成多段。
- 每段输出：
  - `index`：从 0 开始的连续序号。
  - `title`：该教学单元的小节标题（简洁、有教学价值）。
  - `topics_covered`：本段覆盖的 topic（来自输入 `topics`，可多个）。
  - `concepts_introduced`：本段首次引入并定义的概念。只列 `chapter_owns` 中的
    概念；`chapter_uses` 中的概念只引用、不在此重新定义。
  - `learning_goal`：读者读完本段应当掌握什么（一句话）。
- 覆盖完整：每个输入 `topics` 至少被某一段的 `topics_covered` 收录一次。
- 顺序合理：先铺垫、后展开，难度递增；如果有 `prev_brief`，首段应能承接上一章。

`SectionPlan.chapter_id` 与输入一致；`owner_task_id` 以 `:section_plan` 结尾。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> SectionPlan:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        topics = [str(topic).strip() for topic in inp.get("topics", []) if str(topic).strip()]
        draft = SectionPlan(
            chapter_id=ch_id,
            sections=_draft_sections(ch_id, title, topics),
            owner_task_id=f"{ch_id}:section_plan",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SectionPlan,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return SectionPlan.model_validate(result)


def _draft_sections(ch_id: str, title: str, topics: list[str]) -> list[SectionSpec]:
    if not topics:
        return [
            SectionSpec(
                chapter_id=ch_id,
                index=0,
                title=title,
                topics_covered=[],
                concepts_introduced=[],
                learning_goal=f"Understand the core ideas of {title}.",
            )
        ]
    return [
        SectionSpec(
            chapter_id=ch_id,
            index=index,
            title=topic,
            topics_covered=[topic],
            concepts_introduced=[],
            learning_goal=f"Understand {topic}.",
        )
        for index, topic in enumerate(topics)
    ]
