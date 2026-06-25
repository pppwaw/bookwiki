from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import ChapterProposal, StructureResult


class StructureAgent:
    kind: ClassVar[str] = "structure_llm_v2"
    output_model: ClassVar[type[StructureResult]] = StructureResult
    model_key: ClassVar[str] = "structure"
    prompt_name: ClassVar[str] = "structure"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是书籍结构 agent。

根据源摘要创建一个建议的学习结构。
返回一个 `chapters` 数组。章节对象有两种形态：
- **扁平章**（源文本无小节划分）：含 `title`、`topics`、`source_refs`。
- **嵌套章**（源文本含明确小节，如 "9.2 无穷级数"）：含 `title` 和 `sections`，
  不带 `topics`/`source_refs`；`sections` 是子条目数组，
  每个子条目含 `title`、`topics`、`source_refs`。
字段说明：
- `title`：自然的章节标题（人类可读的显示名）。
  - 源文本含明确章节标题/编号时，沿用它（如 "Chapter 6 Point Estimation" 或
    "第 9 章 无穷级数"），允许编号不连续（如缺第 8 章可从 7 跳到 9）。
  - 源文本**没有**可见编号时（如合并章、习题册、附录），用能概括该章内容的描述性标题
    （如 "知识图谱总览"），不必硬加 "Chapter N" 前缀。
  - 标题应在全书内**唯一且具体**；系统会从标题自动派生 URL slug（相同标题会被加后缀去重），
    无需自己写 slug 或内部 ID（如 `ch06`）。
- `topics`：源文本中可见的主题或小节标题列表。
- `source_refs`：该章节（或子节）覆盖的 source_ref 列表，必须与输入完全一致。
- `sections`：章下子节列表（仅嵌套章使用，父级不带 `topics`/`source_refs`）。

**同一章的所有小节必须作为 `sections` 归到唯一一个嵌套章条目下**：
- 章级标题（如 "第9章 无穷级数"）作为父级 `title`，
  其下所有小节（如 "9.2 ..."、"9.3 ..."）作为 `sections` 子条目。
- **禁止**对同一章同时产出"章级摘要条目"和"独立小节条目"——
  即不得出现两个条目覆盖同一章的不同小节。
- 若不同 source 的小节标题共享同一章号（如 "9.2 ..." 和 "9.8 ..."），
  它们必须属于同一个嵌套章。

避免空白的占位章节。
不要包含 goal、scope、evidence、散文段落、Markdown 或代码围栏。
结构应反映真实的源内容，而非通用模板。""",
    )

    async def run(
        self,
        inp: list[dict[str, Any]] | dict[str, Any],
        *,
        model: str,
        runtime: LLMRuntime,
    ) -> StructureResult:
        summaries = inp.get("summaries", []) if isinstance(inp, dict) else inp
        draft = _draft_structure(summaries)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=StructureResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return StructureResult.model_validate(result)


def _draft_structure(summaries: list[dict[str, Any]]) -> StructureResult:
    chapters = _chapter_specs_from_sources(summaries)
    return StructureResult(
        chapters=[
            ChapterProposal(
                title=_display_heading(plan),
                topics=_topic_terms(plan)[:8] or ["Source-grounded overview"],
                source_refs=plan.source_refs,
            )
            for plan in chapters
        ],
    )


@dataclass
class _ChapterPlan:
    chapter_id: str
    title: str
    detected: bool = False
    source_refs: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    key_terms: list[str] = field(default_factory=list)


def _chapter_specs_from_sources(
    summaries: list[dict[str, Any]],
) -> list[_ChapterPlan]:
    if not summaries:
        return [_fallback_plan("ch01", "Foundations"), _fallback_plan("ch02", "Practice")]
    if len(summaries) == 1 and len(summaries[0].get("source_refs") or []) > 1:
        summary = summaries[0]
        refs = list(summaries[0].get("source_refs") or [])
        source_id = str(summary.get("source_id") or "source-1")
        midpoint = max(1, len(refs) // 2)
        return [
            _plan_from_item("ch01", "Foundations", refs[:midpoint], source_id, summary),
            _plan_from_item("ch02", "Advanced Topics", refs[midpoint:], source_id, summary),
        ]

    chapters_by_id: dict[str, _ChapterPlan] = {}
    chapter_order: list[str] = []
    used_ids: set[str] = set()
    for index, item in enumerate(summaries, start=1):
        source_id = str(item.get("source_id") or f"source-{index}")
        refs = list(item.get("source_refs") or [])
        detected_id = item.get("detected_chapter_id")
        chapter_id = str(detected_id) if detected_id else f"ch{index:02d}"
        if not detected_id and chapter_id in used_ids:
            chapter_id = f"ch{index:02d}"
        used_ids.add(chapter_id)
        title = str(item.get("detected_title") or _title_from_source_id(source_id))
        if chapter_id in chapters_by_id:
            plan = chapters_by_id[chapter_id]
            _append_unique(plan.source_refs, refs)
            _append_unique(plan.source_ids, [source_id])
            _append_unique(plan.summaries, _string_list(item.get("summary_md")))
            _append_unique(plan.headings, _string_list(item.get("headings")))
            _append_unique(plan.key_terms, _string_list(item.get("key_terms")))
        else:
            chapter_order.append(chapter_id)
            chapters_by_id[chapter_id] = _plan_from_item(chapter_id, title, refs, source_id, item)
    chapters = [chapters_by_id[chapter_id] for chapter_id in chapter_order]
    return chapters


def _title_from_source_id(source_id: str) -> str:
    return source_id.replace("-", " ").replace("_", " ").title()


def _plan_from_item(
    chapter_id: str,
    title: str,
    refs: list[str],
    source_id: str,
    item: dict[str, Any],
) -> _ChapterPlan:
    return _ChapterPlan(
        chapter_id=chapter_id,
        title=title,
        detected=bool(item.get("detected_chapter_id")),
        source_refs=list(dict.fromkeys(refs)),
        source_ids=[source_id],
        summaries=_string_list(item.get("summary_md")),
        headings=_string_list(item.get("headings")),
        key_terms=_string_list(item.get("key_terms")),
    )


def _fallback_plan(chapter_id: str, title: str) -> _ChapterPlan:
    return _ChapterPlan(chapter_id=chapter_id, title=title)


def _display_heading(plan: _ChapterPlan) -> str:
    chapter = re.match(r"^ch0*(\d+)$", plan.chapter_id)
    if chapter:
        return f"Chapter {int(chapter.group(1))} {plan.title}"
    return f"{plan.chapter_id} {plan.title}"


_KNOWN_TOPIC_PHRASES = (
    "maximum likelihood estimation",
    "method of maximum likelihood",
    "method of moments",
    "moment estimators",
    "sample moments",
    "population moments",
    "point estimation",
    "parameter estimation",
    "unknown parameters",
    "random sample",
    "sampling distribution",
    "statistic's distribution",
    "population distribution",
    "statistical quantities",
    "joint pmf",
    "joint pdf",
    "exponential distribution",
    "negative binomial distribution",
)


def _topic_terms(plan: _ChapterPlan) -> list[str]:
    topics: list[str] = []
    _append_unique(topics, plan.key_terms)
    _append_unique(topics, [_clean_topic(heading) for heading in plan.headings])
    _append_unique(topics, _summary_topics(plan.summaries))
    return topics[:10]


def _summary_topics(summaries: list[str]) -> list[str]:
    topics: list[str] = []
    normalized = _normalize_topic(" ".join(summaries))
    for phrase in _KNOWN_TOPIC_PHRASES:
        if phrase in normalized:
            topics.append(phrase)
    return topics


def _clean_topic(value: str) -> str:
    value = re.sub(r"^summary for [^:]+:\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^chapter\s+\d+\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\([^)]*\)", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .-:")
    if value.lower().startswith("the "):
        value = value[4:]
    return value if 4 <= len(value) <= 80 else ""


def _normalize_topic(text: str) -> str:
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"[-_]+", " ", text)
    return re.sub(r"\s+", " ", text.lower())


def _append_unique(items: list[str], values: list[str]) -> None:
    for value in values:
        value = value.strip()
        if not value:
            continue
        normalized = _normalize_topic(value)
        if any(_normalize_topic(item) == normalized for item in items):
            continue
        items.append(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []
