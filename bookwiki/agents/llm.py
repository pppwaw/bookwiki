from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bookwiki.agents.document import model_to_document, parse_frontmatter_document
from bookwiki.agents.prompting import PromptTemplate, render_prompt
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.utils.logging import get_logger

_LOG = get_logger(__name__)


async def generate_with_llm(
    *,
    runtime: LLMRuntime,
    model: str,
    output_model: type[BaseModel],
    agent_name: str,
    prompt_name: str,
    prompt_template: PromptTemplate,
    inp: Any,
    draft: BaseModel | dict[str, Any],
    allowed_citation_refs: Iterable[str] | None = None,
    image_paths: Sequence[str | Path] | None = None,
    max_attempts: int = 2,
) -> BaseModel:
    prompt = render_prompt(
        prompt_name=prompt_name,
        prompt_template=prompt_template,
        agent_name=agent_name,
        inp=compact_input(inp),
        draft=draft,
        output_model=output_model,
    )
    allowed_refs = set(allowed_citation_refs or [])
    context = {"allowed_citation_refs": allowed_refs} if allowed_refs else None
    return await runtime.generate(
        model=model,
        output_model=output_model,
        system=prompt.system,
        user=prompt.user,
        context=context,
        image_paths=image_paths,
        max_retries=max_attempts,
    )


async def generate_document_with_llm(
    *,
    runtime: LLMRuntime,
    model: str,
    output_model: type[BaseModel],
    agent_name: str,
    prompt_name: str,
    prompt_template: PromptTemplate,
    inp: Any,
    draft: BaseModel,
    body_field: str,
    defaults: dict[str, Any],
    allowed_citation_refs: Iterable[str] | None = None,
    image_paths: Sequence[str | Path] | None = None,
    max_attempts: int = 2,
) -> BaseModel:
    prompt = render_prompt(
        prompt_name=prompt_name,
        prompt_template=prompt_template,
        agent_name=agent_name,
        inp=compact_input(inp),
        draft=draft,
        output_model=output_model,
    )
    allowed_refs = set(allowed_citation_refs or [])
    context = {"allowed_citation_refs": allowed_refs} if allowed_refs else None
    draft_document = model_to_document(draft, body_field=body_field)
    system = (
        f"{prompt.system}\n\n"
        "本次调用是 MDX-direct 文档输出模式：不要返回 JSON；"
        "只返回 YAML frontmatter + raw MDX body。"
    )
    base_user = _document_user_prompt(prompt.user, draft_document=draft_document)
    user = base_user
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        text = await runtime.generate_document(
            model=model,
            system=system,
            user=user,
            image_paths=image_paths,
            max_retries=max_attempts,
        )
        try:
            return parse_frontmatter_document(
                text,
                output_model=output_model,
                body_field=body_field,
                defaults=defaults,
                context=context,
            )
        except ValueError as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                break
            # Rebuild from the ORIGINAL prompt (not cumulative) to avoid snowballing,
            # and include the prior failed document so the model can fix it in place
            # rather than blindly regenerating from scratch.
            user = (
                f"{base_user}\n\n"
                "上一次 MDX-direct 文档无法解析或校验失败。\n"
                f"错误：{exc}\n\n"
                "上一次返回的文档（请在此基础上定点修正，不要从零重写）：\n"
                "```\n"
                f"{_truncate_failed_doc(text)}\n"
                "```\n\n"
                "请修正后重新返回完整文档：开头必须是 YAML frontmatter，"
                "随后是 `---` 与 raw MDX body。"
            )
    assert last_error is not None
    raise last_error


def _truncate_failed_doc(text: str, *, max_chars: int = 20_000) -> str:
    """Keep a failed document small for the retry prompt: head + tail when oversized."""
    if len(text) <= max_chars:
        return text
    head = text[:16_000]
    tail = text[-4_000:]
    return f"{head}\n\n[... truncated {len(text) - max_chars} chars ...]\n\n{tail}"


def compact_input(value: Any, *, max_chars: int = 40_000) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        _LOG.warning("compact_input truncated a %d-char string to %d", len(value), max_chars)
        return value[:max_chars] + "\n\n[truncated]"
    if isinstance(value, dict):
        return {key: compact_input(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_input(item, max_chars=max_chars) for item in value]
    return value


def _document_user_prompt(user: str, *, draft_document: str) -> str:
    return (
        f"{user}\n\n"
        "MDX-direct 输出格式（本段优先级高于上文 JSON 输出要求）：\n"
        "- 不要返回 JSON。\n"
        "- 只返回一个文档：开头是 YAML frontmatter，接着一行 `---`，之后是 raw MDX body。\n"
        "- YAML frontmatter 只放元数据字段；正文放在第二个 `---` 之后，不要放进 YAML 字符串。\n"
        "- 含 LaTeX 反斜杠的 frontmatter 字段必须用 YAML 单引号标量或块标量；"
        "body 中 LaTeX 原样书写，例如 `$\\mu$`，不要 JSON 转义。\n\n"
        "Draft Document:\n"
        "```mdx\n"
        f"{draft_document}\n"
        "```\n\n"
        "Return only the final YAML frontmatter + raw MDX body document."
    )
