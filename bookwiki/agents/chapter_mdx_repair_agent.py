from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import Citation


class ChapterMdxRepairAgent:
    """Rewrite a chapter body that fails MDX compilation, fixing only the unsafe syntax.

    Driven by the ``MDX_PARSE_ERROR`` issues that ``check_node`` raises after compiling
    the rendered chapter with the same parser as the site (``@mdx-js/mdx`` + remark-math).
    The model receives the chapter ``body_md`` plus the compiler diagnostics and wraps the
    offending bare math / comparisons / set notation into proper LaTeX (``n<30`` → ``$n < 30$``,
    ``{z ≥ a}`` → ``$\\{z \\ge a\\}$``) WITHOUT changing the teaching content, figures, or
    identifiers. The repair loop re-renders and re-compiles, so partial fixes converge.
    """

    kind: ClassVar[str] = "chapter_mdx_repair_llm_v1"
    output_model: ClassVar[type[ChapterResult]] = ChapterResult
    model_key: ClassVar[str] = "mdx_repair"
    prompt_name: ClassVar[str] = "chapter_mdx_repair"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 MDX 修复 agent。本章正文在站点 MDX 编译时报错（见输入 `mdx_errors`）。
站点用 MDX + remark-math 解析,常见崩溃原因是正文里有**没放进 `$...$` 的数学构造**:

- 比较式/不等式,如 `n<30`、`x>=0` —— `<`/`>` 被当成 JSX 标签起始 → 崩
- 集合/区间记号,如 `{z ≥ a}`、`[a, b]` —— `{...}` 被当成 JS 表达式 → 崩
- 裸 Unicode 数学符号或希腊字母

你的任务:**只修复导致编译失败的语法**,把这些构造改写成正确的 LaTeX:
- `n<30` → `$n < 30$`;`x>=0` → `$x \\ge 0$`
- `{z ≥ a}` → `$\\{z \\ge a\\}$`;`[a, b]` → `$[a, b]$`
- 裸符号 `μ`/`σ`/`≥` → `$\\mu$`/`$\\sigma$`/`$\\ge$`

严格约束:
- **不要改动教学内容、措辞、结构、小节标题**;只把不安全的数学片段包进 LaTeX。
- **主动扫描整段正文**,把**所有**未包进 `$...$` 的数学/比较/集合记号都修掉——
  编译器一次只报一个错,但你要一次性修干净,不要只改报错指出的那一处。
- **不要删改任何 `<BookFigure ... />` 标签**(原样保留)。
- 保持 `chapter_id`、`title`、`concepts`、`citations` 与输入完全一致;
  `owner_task_id` 以 `:chapter` 结尾。
- 行内公式用 `$...$`,独立公式用 `$$...$$`;不要用 `\\( \\)` 或 `\\[ \\]`。
- 不要引入新的 source_ref;`citations` 的 ref_id 必须仍在 `allowed_source_refs` 中。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ChapterResult:
        ch_id = chapter_id(inp)
        refs = {str(ref) for ref in inp.get("allowed_source_refs", []) if str(ref).strip()}
        draft = ChapterResult(
            chapter_id=ch_id,
            title=str(inp.get("title") or chapter_title(inp)),
            body_md=str(inp.get("body_md") or ""),
            concepts=[str(c) for c in inp.get("concepts", []) if str(c).strip()],
            citations=_draft_citations(inp.get("citations")),
            owner_task_id=str(inp.get("owner_task_id") or f"{ch_id}:chapter"),
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ChapterResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
            allowed_citation_refs=refs or None,
        )
        return ChapterResult.model_validate(result)


def _draft_citations(raw: Any) -> list[Citation]:
    if not isinstance(raw, list):
        return []
    citations: list[Citation] = []
    for item in raw:
        if isinstance(item, dict) and item.get("ref_id") and item.get("quote"):
            citations.append(Citation(ref_id=str(item["ref_id"]), quote=str(item["quote"])))
    return citations
