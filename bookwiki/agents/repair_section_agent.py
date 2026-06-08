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
from bookwiki.agents.section_agent import section_owner_task_id
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.section import SectionResult


class RepairSectionAgent:
    """Repair a single section flagged by ``validate_section``.

    Receives the previously generated (failing) ``SectionResult`` plus the list
    of validation issues, and returns a corrected ``SectionResult`` with the
    same ``chapter_id`` / ``section_index`` / ``owner_task_id``. The deterministic
    draft echoes the previous output, so under ``TestLLMRuntime`` an unfixable
    fixture stays failing and exercises the fallback path; a recording runtime
    can return a corrected version to exercise the success path.
    """

    kind: ClassVar[str] = "section_repair_llm_v1"
    output_model: ClassVar[type[SectionResult]] = SectionResult
    model_key: ClassVar[str] = "section_repair"
    prompt_name: ClassVar[str] = "section_repair"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是逐段修复 agent。给定某一段已生成但**未通过校验**的正文
（`previous_section`）以及校验问题列表（`issues`），请产出修正后的同一段正文。

修复原则：
- 逐条解决 `issues`：
  - 「未知引用」：把 `citations` 改为只引用 `allowed_source_refs` 中的 ref_id，
    不要发明新的 source_ref。
  - 「重复定义他章概念」：对 `chapter_uses` 中的概念改为引用而非重新定义。
  - 「术语漂移」：把 `alias_map` 中的变体改写为其规范名（canonical）。
- 仅做必要的最小修改，保持本段教学意图与覆盖范围不变；不要扩写到其他段的内容。
- `body_md` 不含章节级 `# 一级标题`，也不重复本段小节标题；行内公式用 $...$，
  独立公式用 $$...$$。
- 保持 `chapter_id`、`section_index`、`title`、`owner_task_id` 与
  `previous_section` 完全一致。`figure_requests` 留空。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> SectionResult:
        ch_id = chapter_id(inp)
        previous = inp.get("previous_section", {})
        previous = previous if isinstance(previous, dict) else {}
        refs = source_refs(inp)
        index = _section_index(previous)
        draft = SectionResult(
            chapter_id=str(previous.get("chapter_id") or ch_id),
            section_index=index,
            title=str(previous.get("title") or chapter_title(inp)),
            body_md=str(previous.get("body_md") or ""),
            concepts=[str(c) for c in previous.get("concepts", []) if str(c).strip()],
            citations=_draft_citations(previous, inp),
            figure_requests=[],
            owner_task_id=str(previous.get("owner_task_id") or section_owner_task_id(ch_id, index)),
        )
        llm_input = _content_input(inp, refs)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SectionResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=llm_input,
            draft=draft,
            allowed_citation_refs=refs,
        )
        return SectionResult.model_validate(result)


def _draft_citations(previous: dict[str, Any], inp: dict[str, Any]) -> list[Any]:
    raw = previous.get("citations")
    if isinstance(raw, list) and raw:
        return raw
    return [citation(inp)]


def _section_index(previous: dict[str, Any]) -> int:
    try:
        index = int(previous.get("section_index", 0))
    except (TypeError, ValueError):
        return 0
    return index if index >= 0 else 0


def _content_input(inp: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    payload = {key: value for key, value in inp.items() if key != "source_md"}
    payload["document_xml"] = chapter_document(inp)
    payload["allowed_source_refs"] = sorted(refs)
    return payload
