from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_document_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptMdxRepairAgent:
    """Rewrite a concept page body that fails MDX compilation, fixing only unsafe syntax.

    Concept counterpart of :class:`ChapterMdxRepairAgent`. Driven by the
    ``MDX_PARSE_ERROR`` issues that ``check_node`` raises after compiling the rendered
    ``concepts/*.mdx`` with the same parser as the site (``@mdx-js/mdx`` + remark-math).
    The model receives the concept ``body_md`` plus the compiler diagnostics and fixes the
    two break classes seen in generated concept pages WITHOUT changing teaching content:

    - bare math (``{\\frac{a}{b}}``, ``n<30``, ``μ``) that must be wrapped in ``$...$``;
    - stray inline citation tags like ``<cite ref_id="...">`` (often unclosed) that the
      model should never emit — citations belong in the ``citations`` array, so these are
      removed and the wrapped text is kept as plain prose.
    """

    kind: ClassVar[str] = "concept_mdx_repair_llm_v1"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "mdx_repair"
    prompt_name: ClassVar[str] = "concept_mdx_repair"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 MDX 修复 agent。本概念页正文在站点 MDX 编译时报错（见输入 `mdx_errors`）。
站点用 MDX + remark-math 解析,概念页常见的两类崩溃原因是:

1. **没放进 `$...$` 的数学构造**:
   - 比较式/不等式,如 `n<30`、`x>=0` —— `<`/`>` 被当成 JSX 标签起始 → 崩
   - 集合/区间/分数等花括号记号,如 `{z ≥ a}`、`{\\frac{a}{b}}` —— `{...}` 被当成 JS 表达式 → 崩
   - 裸 Unicode 数学符号或希腊字母、裸 LaTeX 命令(`\\leq`、`\\frac` 等不在 `$...$` 内)
2. **臆造的内联引用标签**,如 `<cite ref_id="...">…</cite>`(常常还未闭合)——
   这种标签**根本不该出现**,引用只能放在 `citations` 数组里。

你的任务:**只修复导致编译失败的语法**:
- 把数学构造改写成正确的 LaTeX:`n<30` → `$n < 30$`;`{\\frac{a}{b}}` → `$\\frac{a}{b}$`;
  `\\leq` → `$\\leq$`;裸符号 `μ`/`σ`/`≥` → `$\\mu$`/`$\\sigma$`/`$\\ge$`。
- **删除所有内联 `<cite ...>` / `</cite>` 标签**,保留其包裹的正文文字(把它当作普通正文留下);
  不要把这些引用搬进 `citations`——`citations` 保持与输入一致即可。

严格约束:
- **不要改动教学内容、措辞、结构**;只把不安全的数学包进 `$...$`、删掉内联引用标签。
- **主动扫描整段正文**,把**所有**这两类问题一次性修干净——编译器一次只报一个错。
- 保持 `name`、`summary_md`、`related`、`citations` 与输入完全一致;
  `owner_task_id` 与输入一致(形如 `concept:<name>`)。
- 行内公式用 `$...$`,独立公式用 `$$...$$`;不要用 `\\( \\)` 或 `\\[ \\]`。
- 不要引入新的 source_ref;`citations` 的 ref_id 必须仍在 `allowed_source_refs` 中。

输出格式（MDX-direct）：
- 只返回 YAML frontmatter + raw MDX body，不要返回 JSON。
- frontmatter 字段：`name`、`summary_md`、`related`、`citations`。
- 不要在 frontmatter 中输出 `owner_task_id`；系统会用确定性默认值注入。
- `summary_md`、citation quote 等含 LaTeX 反斜杠时必须使用 YAML 单引号标量或块标量。
- 第二个 `---` 后直接写 raw MDX `body_md`；正文 LaTeX 原样书写，如 `$\\mu$`，不要 JSON 转义。""",
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
        result = await generate_document_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
            body_field="body_md",
            defaults={"owner_task_id": str(inp.get("owner_task_id") or f"concept:{name}")},
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
