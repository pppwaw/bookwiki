from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.quality import QualityReport


class QualityCheckAgent:
    """Detect high-precision semantic quality defects in generated body markdown."""

    kind: ClassVar[str] = "quality_check_llm_v1"
    output_model: ClassVar[type[QualityReport]] = QualityReport
    model_key: ClassVar[str] = "quality_check"
    prompt_name: ClassVar[str] = "quality_check"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 BookWiki 的语义质量检查 agent。输入是一段已生成的章节或概念页 `body_md`。

目标语言是 `language`。你的任务只检查一类问题:`language_leak`。

只在以下情况报告:
- 源材料中的英文整句或长英文片段没有被消化翻译,直接混进目标语言正文。
- 中文与英文生硬粘连成 run-on,例如“查得select the cutoff value to control ...”。

绝对不要报告这些合法英文:
- 术语括注,例如 `置信区间 (Confidence Interval, CI)`。
- 数学公式、LaTeX 或任何 `$...$` / `$$...$$` 内的内容。
- citation/source quote、参考来源标题、专有名词、缩写、变量名、代码、URL。
- `<BookFigure ... />`、`<PreviewLink ...>` 等 MDX/JSX 标签。

严格约束:
- `findings[].category` 只能是 `language_leak`。
- 每个 `findings[].quote` 必须是 `body_md` 中逐字存在的连续子串,不能改写、概括或补全。
- explanation 简短说明为什么这是未消化源语言或中英粘连。
- 高精度优先:不确定就返回空 findings;宁可漏报,不要误报。
- 不要输出修复后的正文;这里只做检查。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> QualityReport:
        draft = QualityReport(
            owner_task_id=str(inp.get("owner_task_id") or ""),
            findings=[],
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=QualityReport,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return QualityReport.model_validate(result)
