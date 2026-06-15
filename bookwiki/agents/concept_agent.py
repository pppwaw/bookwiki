from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_document_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptAgent:
    kind: ClassVar[str] = "concept_llm_v1"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "concept"
    prompt_name: ClassVar[str] = "concept"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是概念页 agent。用费曼式的口吻写一个聚焦、面向学习者的概念页：把
这个概念解释给一位尚未读过相关章节、但充满好奇的同伴听。

页面形态：
- 用通俗语言给出一句“它是什么”的开场，若有助于建立直觉就配一个贴切的类比。
- 一段简短的“为什么重要”：它解决什么问题、出现在哪里、它取代了哪种错误直觉。
- 机制：精确的定义或公式，每个符号都加以命名并读出。当可用语境支持时，展示
  一个最小化的完整示例或情景。
- 常见混淆与相邻概念，以及与 related 中任何内容的简要对比。

规则：
- 用一段紧凑的 1-2 句预览填充 summary_md，用于悬停卡片。它应直接定义该概念，
  避免冗长示例、标题和引用。
- 写一个适合 Fumadocs MDX 学习站点的简洁概念页。
- 解释该概念、它为何重要，以及它与所链接章节的关系。
- related：从输入的 `glossary`（全书概念规范名清单）里挑 3-8 个与本概念最紧密相关的
  概念，**逐字照抄 glossary 中的规范名**，不要改写/缩写/翻译/自造；紧密相关的不足 3 个就
  少给，绝不编造 glossary 中没有的名称，也不要把本概念自己列进去。
- 保持引用扎根于可用的章节/源语境。
- 引用只能放进 `citations` 数组；正文里**绝不要**输出任何内联引用标签（如
  `<cite ref_id="...">…</cite>`）或自造的 JSX/HTML 标签——这类标签会破坏 MDX 编译。
- 不要发明交叉链接或事实。

数学：
- 使用 Markdown 数学语法：行内公式用 $...$，独立展示公式用 $$...$$。
- 一切数学符号、关系式、希腊字母、分数、集合/区间与花括号记号都必须放进 $...$；
  绝不要在正文里直接写裸花括号（如 `{\\frac{a}{b}}`、`{x ≥ a}`）、裸 LaTeX 命令
  （如 `\\frac`、`\\leq`）或裸 Unicode 数学符号——它们会破坏 MDX 编译。
- 不要使用 \\( ... \\) 或 \\[ ... \\] 数学定界符。

输出格式（MDX-direct）：
- 只返回 YAML frontmatter + raw MDX body，不要返回 JSON。
- frontmatter 字段：`name`、`summary_md`、`related`、`citations`。
- 不要在 frontmatter 中输出 `owner_task_id`；系统会用确定性默认值注入。
- `summary_md`、citation quote 等含 LaTeX 反斜杠时必须使用 YAML 单引号标量或块标量。
- 第二个 `---` 后直接写 raw MDX `body_md`；正文 LaTeX 原样书写，如 `$\\mu$`，不要 JSON 转义。""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ConceptResult:
        name = str(inp.get("canonical") or inp.get("name") or "Concept")
        chapters = [str(ch) for ch in inp.get("source_chapter_ids", ["ch01"])]
        contexts = [item for item in inp.get("chapter_contexts", []) if isinstance(item, dict)]
        citations = _context_citations(contexts)
        allowed_refs = _context_source_refs(contexts)
        draft = ConceptResult(
            name=name,
            summary_md=_draft_summary(name, chapters, contexts),
            body_md=_draft_body(name, chapters, contexts),
            related=[],
            citations=citations,
            owner_task_id=f"concept:{name}",
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
            defaults={"owner_task_id": f"concept:{name}"},
            allowed_citation_refs=allowed_refs,
        )
        return ConceptResult.model_validate(result)


def _draft_summary(name: str, chapters: list[str], contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return f"{name} is a reconciled concept linked from {', '.join(chapters)}."
    chapter_titles = ", ".join(
        str(item.get("title") or item.get("chapter_id")) for item in contexts
    )
    return f"{name} is a key concept used in {chapter_titles}."


def _draft_body(name: str, chapters: list[str], contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return f"{name} is a reconciled concept linked from {', '.join(chapters)}."
    chapter_titles = ", ".join(
        str(item.get("title") or item.get("chapter_id")) for item in contexts
    )
    return f"{name} is a reconciled concept linked from {chapter_titles}."


def _context_citations(contexts: list[dict[str, Any]]) -> list[Citation]:
    for context in contexts:
        for item in context.get("citations", []):
            ref_id = str(item.get("ref_id", "")).strip()
            quote = str(item.get("quote", "")).strip()
            if ref_id and quote:
                return [Citation(ref_id=ref_id, quote=quote)]
        for ref_id in _source_refs(str(context.get("source_md", ""))):
            return [Citation(ref_id=ref_id, quote="source context")]
    return []


def _context_source_refs(contexts: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for context in contexts:
        for item in context.get("citations", []):
            ref_id = str(item.get("ref_id", "")).strip()
            if ref_id:
                refs.add(ref_id)
        refs.update(_source_refs(str(context.get("source_md", ""))))
    return refs


def _source_refs(source_md: str) -> list[str]:
    import re

    return re.findall(r"source_ref:\s*([^\s>]+)", source_md)
