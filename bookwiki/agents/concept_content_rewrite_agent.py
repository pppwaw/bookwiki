from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptContentRewriteAgent:
    """Rewrite only flagged semantic-quality spans in a concept page body."""

    kind: ClassVar[str] = "concept_content_rewrite_llm_v1"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "quality_rewrite"
    prompt_name: ClassVar[str] = "concept_content_rewrite"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 BookWiki 的概念页内容重写 agent。
输入 `quality_findings` 标出了正文中的语义质量问题。

你的任务:只把每个 `quality_findings[].quote` 对应的原文片段改写成干净、自然的
目标语言 prose。

严格约束:
- 只改 `quality_findings[].quote` 命中的片段;除此以外,正文必须 byte-for-byte 保持不变。
- 不要改动教学内容、结构、`summary_md`、related 列表、Markdown/MDX 结构。
- 不要删改任何 `<BookFigure ... />`、`<PreviewLink ...>`、公式、代码、链接或引用。
- 保持 `name`、`summary_md`、`related`、`citations`、`owner_task_id` 与输入完全一致。
- 不要引入新的 source_ref;`citations` 的 ref_id 必须仍在 `allowed_source_refs` 中。
- 如果某个 quote 不在正文中,不要臆造位置;保持正文其它内容不变。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ConceptResult:
        name = str(inp.get("name") or "Concept")
        refs = {str(ref) for ref in inp.get("allowed_source_refs", []) if str(ref).strip()}
        draft = ConceptResult(
            name=name,
            summary_md=str(inp.get("summary_md") or ""),
            body_md=str(inp.get("body_md") or ""),
            related=[str(item) for item in inp.get("related", []) if str(item).strip()],
            citations=_draft_citations(inp.get("citations")),
            owner_task_id=str(inp.get("owner_task_id") or f"concept:{name}"),
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
            allowed_citation_refs=refs or None,
        )
        return ConceptResult.model_validate(result)


def _draft_citations(raw: Any) -> list[Citation]:
    if not isinstance(raw, list):
        return []
    citations: list[Citation] = []
    for item in raw:
        if isinstance(item, dict) and item.get("ref_id") and item.get("quote"):
            citations.append(Citation(ref_id=str(item["ref_id"]), quote=str(item["quote"])))
    return citations
