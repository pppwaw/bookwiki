"""Surgical MDX repair via an Anthropic-style edit-tool loop.

Replaces the whole-document rewrite agents (``ChapterMdxRepairAgent`` /
``ConceptMdxRepairAgent``) for the *syntax* repair class (``MDX_PARSE_ERROR``):
regenerating a 600-line body to fix a one-line fence is unstable (drift,
truncation, whack-a-mole). Instead the model gets two tools over an in-memory
copy of ``body_md`` —

* ``view(start_line, end_line)``    — numbered ``cat -n`` style excerpt
* ``str_replace(old_str, new_str)`` — exact, must-match-exactly-once edit

— and after every successful edit the host re-runs the bundled MDX validator
(same parser config as the site) and feeds the remaining errors back, so the
loop converges on compiling MDX without the model ever re-emitting the body.
The final structured answer is a tiny status object; the repaired body is taken
from the editor state and the chapter/concept *metadata is passed through
unchanged* from the input (the rewrite agents had to reproduce citations etc.,
a needless fabrication risk).

Semantic quality rewrites (language leaks) stay on the whole-document
``*ContentRewriteAgent`` path; per the DROP-over-fabrication philosophy there is
deliberately **no** whole-rewrite fallback when this loop fails to converge —
the fewest-error snapshot is returned and the macro check/repair accounting
handles the rest.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar, Literal

from pydantic import Field

from bookwiki.agents.llm import compact_input
from bookwiki.agents.prompting import PromptTemplate, render_prompt
from bookwiki.checkers.mdx_validator import validate_mdx
from bookwiki.scheduler.llm import LLMRuntime, ToolLoopExceeded
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import VersionedModel
from bookwiki.schemas.concept import ConceptResult
from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

_VIEW_MAX_LINES = 120
_VIEW_MAX_CHARS = 20_000
_ERROR_WINDOW = 25
_MAX_PROMPT_WINDOWS = 3
_MAX_TOOL_ROUNDS = 30
_MAX_EDITS = 30
_MAX_STALE_EDITS = 3

EDIT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "view",
            "description": (
                "查看正文的某个行区间，返回带 1-based 行号的文本"
                f"（单次最多 {_VIEW_MAX_LINES} 行）。编辑前先 view 确认精确内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer", "description": "起始行（1-based，含）"},
                    "end_line": {"type": "integer", "description": "结束行（1-based，含）"},
                },
                "required": ["start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace",
            "description": (
                "把正文中与 old_str 完全一致（含空白/换行）且唯一的一处替换为 new_str。"
                "0 处或多处匹配都会报错并且不做任何修改。"
                "每次成功替换后系统会自动重新编译 MDX 并返回剩余错误。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_str": {
                        "type": "string",
                        "description": "要替换的原文片段，必须逐字符匹配且在全文唯一",
                    },
                    "new_str": {"type": "string", "description": "替换后的内容，可为空串表示删除"},
                },
                "required": ["old_str", "new_str"],
            },
        },
    },
]


class _EditRepairOutcome(VersionedModel):
    """Tiny final answer for the tool loop; the body itself lives in the editor."""

    status: Literal["fixed", "partial", "gave_up"] = "gave_up"
    notes: str = Field(default="")


class MdxBodyEditor:
    """Pure in-memory editor with Anthropic ``str_replace`` semantics."""

    def __init__(self, text: str) -> None:
        self.text = text

    def view(self, start_line: int, end_line: int) -> dict[str, Any]:
        lines = self.text.split("\n")
        total = len(lines)
        try:
            start = max(1, int(start_line))
            end = min(total, int(end_line))
        except (TypeError, ValueError):
            return {"ok": False, "error": "start_line/end_line must be integers"}
        if end < start:
            return {"ok": False, "error": f"end_line {end} is before start_line {start}"}
        end = min(end, start + _VIEW_MAX_LINES - 1)
        numbered = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
        if len(numbered) > _VIEW_MAX_CHARS:
            numbered = numbered[:_VIEW_MAX_CHARS] + "\n... [view truncated]"
        return {"ok": True, "total_lines": total, "content": numbered}

    def str_replace(self, old_str: str, new_str: str) -> dict[str, Any]:
        if not old_str:
            return {"ok": False, "error": "old_str must be a non-empty string"}
        count = self.text.count(old_str)
        if count == 0:
            return {
                "ok": False,
                "error": (
                    "No match found for replacement: old_str did not appear verbatim. "
                    "Use view to copy the exact text (including whitespace) and retry."
                ),
            }
        if count > 1:
            lines = [
                idx + 1
                for idx, line in enumerate(self.text.split("\n"))
                if old_str.split("\n", 1)[0] in line
            ][:8]
            return {
                "ok": False,
                "error": (
                    f"Found {count} matches for old_str (around lines {lines}). "
                    "Provide a longer snippet with more surrounding context to make it unique."
                ),
            }
        self.text = self.text.replace(old_str, new_str, 1)
        return {"ok": True}


_REPAIR_PROMPT = PromptTemplate(
    body="""你是 MDX 定点修复 agent。正文在站点 MDX 编译时报错（见 `mdx_errors`，
站点用 MDX + remark-math 解析）。你的任务是用工具做**最小的外科手术式修改**，
让正文重新编译通过，绝不重写整篇。

常见崩溃原因：
- 行间公式围栏不规范：`$$f(x)=` 开栏同一行带内容、`...},$$` 闭栏前同一行有内容、
  或 `$$内容$$` 后面直接接正文 —— 把 `$$` 改为独占一行；
- 没放进 `$...$` 的比较式/集合记号，如 `n<30`、`{z ≥ a}` —— 包进 LaTeX；
- 裸 `{...}` 被当作 JS 表达式、未闭合的标签。

工作流程：
1. 先用 `view` 查看报错行附近（输入里已给出窗口，可再看更大范围确认上下文）。
2. 用 `str_replace` 做一处定点修改：old_str 必须逐字符唯一匹配（先 view 再复制）。
3. 每次成功替换后系统会返回 `remaining_mdx_errors`；据此继续修，直到为空。
4. 修完（或系统提示停止）后**停止调用工具**，返回最终 JSON：
   `status`（fixed=已无错误 / partial=减少了但未清零 / gave_up）和一句 `notes`。

严格约束：
- 只修导致编译失败的语法；**不要**改动教学内容、措辞、标题、
  `<BookFigure ... />`、`<PreviewLink ...>`、引用注释，也不要改动
  `<QuizBlock>`/`<QuizItem>`/`<QuizItemSlot ... />` 测验标签。
- 不要删除大段内容；优先调整定界符与转义。
- 最终 JSON 里**不要**返回正文。""",
)


def _error_lines(mdx_errors: list[str]) -> list[int]:
    lines: list[int] = []
    for error in mdx_errors:
        match = re.search(r"line (\d+)", error)
        if match:
            lines.append(int(match.group(1)))
    return lines


def _error_windows(editor: MdxBodyEditor, mdx_errors: list[str]) -> str:
    """Render numbered ±N-line excerpts around the first few error lines."""
    total = len(editor.text.split("\n"))
    ranges: list[tuple[int, int]] = []
    for line in _error_lines(mdx_errors)[:_MAX_PROMPT_WINDOWS]:
        start, end = max(1, line - _ERROR_WINDOW), min(total, line + _ERROR_WINDOW)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    blocks = []
    for start, end in ranges:
        viewed = editor.view(start, end)
        if viewed.get("ok"):
            blocks.append(str(viewed["content"]))
    return "\n...\n".join(blocks)


async def repair_body_with_edit_tools(
    *,
    body_md: str,
    mdx_errors: list[str],
    model: str,
    runtime: LLMRuntime,
    agent_name: str,
    doc_label: str,
    language: str | None = None,
) -> tuple[str, list[str]]:
    """Run the bounded edit-tool loop; return ``(best_body, remaining_errors)``.

    ``best`` is the fewest-validator-errors snapshot seen, so a late bad edit can
    never clobber an earlier better state. No whole-rewrite fallback by design.
    """
    editor = MdxBodyEditor(body_md)
    best_body = body_md
    best_errors = list(mdx_errors)
    edits = 0
    stale_edits = 0

    async def tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal best_body, best_errors, edits, stale_edits
        if name == "view":
            return editor.view(args.get("start_line", 1), args.get("end_line", 1))
        if name != "str_replace":
            return {"ok": False, "error": f"unknown tool {name!r}"}
        if edits >= _MAX_EDITS:
            return {
                "ok": False,
                "error": "edit budget exhausted; stop calling tools and return the final answer",
            }
        applied = editor.str_replace(str(args.get("old_str", "")), str(args.get("new_str", "")))
        if not applied.get("ok"):
            return applied
        edits += 1
        errors = await asyncio.to_thread(validate_mdx, editor.text)
        if len(errors) < len(best_errors):
            best_body, best_errors = editor.text, errors
            stale_edits = 0
        else:
            stale_edits += 1
        payload: dict[str, Any] = {
            "ok": True,
            "error_count": len(errors),
            "remaining_mdx_errors": errors[:8],
        }
        if not errors:
            payload["done"] = "body compiles cleanly; stop calling tools and return status=fixed"
        elif stale_edits >= _MAX_STALE_EDITS:
            payload["warning"] = (
                "recent edits are not reducing errors; stop calling tools and return the answer"
            )
        return payload

    prompt = render_prompt(
        prompt_name="mdx_edit_repair",
        prompt_template=_REPAIR_PROMPT,
        agent_name=agent_name,
        inp=compact_input(
            {
                "doc": doc_label,
                "language": language,
                "mdx_errors": mdx_errors,
                "total_lines": len(body_md.split("\n")),
                "error_windows": _error_windows(editor, mdx_errors),
            },
            model=model,
        ),
        draft=_EditRepairOutcome(status="gave_up", notes=""),
        output_model=_EditRepairOutcome,
    )
    try:
        outcome = await runtime.generate_with_tools(
            model=model,
            output_model=_EditRepairOutcome,
            system=prompt.system,
            user=prompt.user,
            tools=EDIT_TOOLS,
            tool_executor=tool_executor,
            max_tool_rounds=_MAX_TOOL_ROUNDS,
        )
        status = _EditRepairOutcome.model_validate(outcome).status
    except ToolLoopExceeded:
        LOGGER.warning(
            "mdx edit repair tool loop exceeded for %s; keeping best snapshot", doc_label
        )
        status = "partial"

    if best_errors:
        LOGGER.warning(
            "mdx edit repair for %s did not fully converge (status=%s, %d error(s) remain)",
            doc_label,
            status,
            len(best_errors),
        )
    return best_body, best_errors


class ChapterMdxEditRepairAgent:
    """Fix a chapter body's MDX compile errors via surgical edit tools (INLINE, pre-integrate).

    Used during generation where the artifact ``body_md`` is the unit of truth; frontmatter
    metadata (title/concepts/citations/owner) is passed through unchanged, only ``body_md``
    is edited. Post-``integrate`` repair uses ``MdxEditRepairAgent`` on the rendered ``.mdx``.
    """

    kind: ClassVar[str] = "chapter_mdx_repair_llm_v2"
    output_model: ClassVar[type[ChapterResult]] = ChapterResult
    model_key: ClassVar[str] = "mdx_repair"
    prompt_name: ClassVar[str] = "mdx_edit_repair"
    prompt_template: ClassVar[PromptTemplate] = _REPAIR_PROMPT

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ChapterResult:
        ch_id = str(inp.get("chapter_id") or "")
        body, _remaining = await repair_body_with_edit_tools(
            body_md=str(inp.get("body_md") or ""),
            mdx_errors=[str(item) for item in inp.get("mdx_errors", [])],
            model=model,
            runtime=runtime,
            agent_name=self.__class__.__name__,
            doc_label=f"chapter {ch_id}",
            language=inp.get("language"),
        )
        return ChapterResult.model_validate(
            {
                "chapter_id": ch_id,
                "title": str(inp.get("title") or ch_id),
                "body_md": body,
                "concepts": list(inp.get("concepts") or []),
                "citations": list(inp.get("citations") or []),
                "owner_task_id": str(inp.get("owner_task_id") or f"{ch_id}:chapter"),
            }
        )


class ConceptMdxEditRepairAgent:
    """Fix a concept body's MDX compile errors via surgical edit tools (INLINE, pre-integrate)."""

    kind: ClassVar[str] = "concept_mdx_repair_llm_v2"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "mdx_repair"
    prompt_name: ClassVar[str] = "mdx_edit_repair"
    prompt_template: ClassVar[PromptTemplate] = _REPAIR_PROMPT

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ConceptResult:
        name = str(inp.get("name") or "")
        body, _remaining = await repair_body_with_edit_tools(
            body_md=str(inp.get("body_md") or ""),
            mdx_errors=[str(item) for item in inp.get("mdx_errors", [])],
            model=model,
            runtime=runtime,
            agent_name=self.__class__.__name__,
            doc_label=f"concept {name}",
            language=inp.get("language"),
        )
        return ConceptResult.model_validate(
            {
                "name": name,
                "summary_md": str(inp.get("summary_md") or ""),
                "body_md": body,
                "related": list(inp.get("related") or []),
                "citations": list(inp.get("citations") or []),
                "owner_task_id": str(inp.get("owner_task_id") or f"concept:{name}"),
            }
        )


class MdxRepairResult(VersionedModel):
    """The repaired ``.mdx`` text (the file is edited in place — no metadata round-trip)."""

    mdx: str


class MdxEditRepairAgent:
    """Fix a rendered ``.mdx`` file's compile errors via surgical edit tools.

    Edits the ``.mdx`` text DIRECTLY (not the source ``body_md`` artifact), so the error
    line numbers align with what ``check`` compiled and the agent can reach constructs that
    only exist after ``integrate`` (frontmatter, injected ``<PreviewLink>``/quiz/figures).
    The caller writes the returned text back to the ``.mdx`` and routes to ``check``.
    """

    kind: ClassVar[str] = "mdx_edit_repair_llm_v3"
    output_model: ClassVar[type[MdxRepairResult]] = MdxRepairResult
    model_key: ClassVar[str] = "mdx_repair"
    prompt_name: ClassVar[str] = "mdx_edit_repair"
    prompt_template: ClassVar[PromptTemplate] = _REPAIR_PROMPT

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> MdxRepairResult:
        repaired, _remaining = await repair_body_with_edit_tools(
            body_md=str(inp.get("mdx") or ""),
            mdx_errors=[str(item) for item in inp.get("mdx_errors", [])],
            model=model,
            runtime=runtime,
            agent_name=self.__class__.__name__,
            doc_label=str(inp.get("doc_label") or "mdx"),
            language=inp.get("language"),
        )
        return MdxRepairResult(mdx=repaired)
