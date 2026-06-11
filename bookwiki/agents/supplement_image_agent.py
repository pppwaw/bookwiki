from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import compact_input
from bookwiki.agents.prompting import PromptTemplate, render_prompt
from bookwiki.scheduler.llm import LLMRuntime, ToolExecutor
from bookwiki.schemas.figure import ImageSupplementResult

# OpenAI-style function specs exposed to the model via LiteLLM tool calling.
FIGURE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_plot",
            "description": (
                "Render a matplotlib figure from Python code. The code runs in an "
                "isolated sandbox with numpy as np pre-imported and seeded; leave an "
                "open figure or call plt.savefig. Returns {ok, image_path, error}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Self-contained matplotlib code producing one figure.",
                    }
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_figure",
            "description": "Check a generated image is usable. Returns {ok, error}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Path returned by run_plot."}
                },
                "required": ["image_path"],
            },
        },
    },
]


class SupplementImageAgent:
    """Generate a missing chapter figure by writing matplotlib code via tool calls.

    Isolated from the prose ``SectionAgent``: this agent runs a function-calling
    loop (``run_plot`` / ``verify_figure``) instead of plain JSON-mode, so the
    risk of mixing a tool loop with structured prose output stays contained here.
    The host-side ``tool_executor`` (built in ``generate.sections``) actually
    runs the sandboxed plot and owns the output path; this agent returns a
    structured :class:`ImageSupplementResult` describing the outcome.
    """

    kind: ClassVar[str] = "supplement_image_llm_v1"
    output_model: ClassVar[type[ImageSupplementResult]] = ImageSupplementResult
    model_key: ClassVar[str] = "supplement_image"
    prompt_name: ClassVar[str] = "supplement_image"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是补图 agent。本章某一段需要一张源文档里没有的图示，你的任务是用
matplotlib **写代码并调用 `run_plot` 工具**把它画出来。

输入提供：`figure_ref`（图的稳定标识，正文里已用 <BookFigure id="<figure_ref>" />
占位）、`rationale`（这段需要什么图、要表达什么）、`section_title`、可选的
`chapter_context`。

工作流程：
1. 想清楚最能解释该知识点的图（折线/柱状/散点/示意），写出**自包含**的 matplotlib
   代码：可用 `np`（已预导入并 seed）、`matplotlib.pyplot`。坐标轴、标题、图例都用
   清晰的英文标注（字体锁定 DejaVu Sans）。
2. 调用 `run_plot(code=...)`。若返回 `ok=false`，根据 `error` 修正代码后重试（最多几次）。
3. 成功后，返回 `ImageSupplementResult`：`ok=true`，`caption` 写一句话说明这张图
   （面向学习者），`figure_ref` 与输入一致。

约束：
- 不要访问网络、文件系统或子进程；只画图。
- 如果实在画不出有意义的图，返回 `ok=false` 并在 `error` 里简述原因，不要硬编造。
- `chapter_id`、`section_index`、`figure_ref` 与输入保持一致；`owner_task_id` 形如
  `<chapter_id>:section:<3 位序号>:figure`。""",
    )

    async def run(
        self,
        inp: dict[str, Any],
        *,
        model: str,
        runtime: LLMRuntime,
        tool_executor: ToolExecutor,
    ) -> ImageSupplementResult:
        ch_id = str(inp.get("chapter_id") or "")
        index = _index(inp)
        figure_ref = str(inp.get("figure_ref") or "")
        draft = ImageSupplementResult(
            chapter_id=ch_id,
            section_index=index,
            figure_ref=figure_ref,
            ok=False,
            caption=str(inp.get("rationale") or ""),
            error="",
            owner_task_id=f"{ch_id}:section:{index:03d}:figure",
        )
        prompt = render_prompt(
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            agent_name=self.__class__.__name__,
            inp=compact_input(inp),
            draft=draft,
            output_model=ImageSupplementResult,
        )
        result = await runtime.generate_with_tools(
            model=model,
            output_model=ImageSupplementResult,
            system=prompt.system,
            user=prompt.user,
            tools=FIGURE_TOOLS,
            tool_executor=tool_executor,
            # 配图是迭代式：写代码→run_plot→看 error 改→重跑→verify，
            # 单次 plot 重试 + verify 就轻易超 4 轮；给足迭代空间（循环仍有界）。
            # 24 轮对正常迭代足够宽裕，又能防止行为异常的模型空转烧 token。
            max_tool_rounds=24,
        )
        return ImageSupplementResult.model_validate(result)


def _index(inp: dict[str, Any]) -> int:
    try:
        index = int(inp.get("section_index", 0))
    except (TypeError, ValueError):
        return 0
    return index if index >= 0 else 0
