from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bookwiki.agents.document import model_to_document, parse_frontmatter_document
from bookwiki.agents.prompting import PromptTemplate, render_prompt
from bookwiki.scheduler.llm import LLMRuntime, count_text_tokens, input_token_budget
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
        inp=compact_input(inp, model=model),
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
    max_attempts: int = 4,
) -> BaseModel:
    prompt = render_prompt(
        prompt_name=prompt_name,
        prompt_template=prompt_template,
        agent_name=agent_name,
        inp=compact_input(inp, model=model),
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
                "随后是 `---` 与 raw MDX body。\n"
                "YAML frontmatter 转义要点（这是上次失败的常见原因）：\n"
                "- 单引号标量内部的单引号必须写成两个单引号（`''`），"
                "例如 `quote: 'KE = ½ m|r''(t)|²'`。\n"
                "- 含反斜杠（LaTeX）或特殊字符的值优先用单引号标量或块标量（`|`），"
                "不要用双引号标量。\n"
                "- 若值里同时含单引号和反斜杠，改用块标量：`quote: |` 换行后缩进写原文。\n"
                "- body 中的 LaTeX 原样书写（如 `$\\mu$`），不要 JSON 转义。"
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


_TRUNCATION_SUFFIX = "\n\n[truncated]"


def compact_input(value: Any, *, model: str, max_tokens: int | None = None) -> Any:
    """Defensively cap each string field of ``value`` to a token budget.

    The budget defaults to the model's input headroom (``input_token_budget``:
    context window minus reserved output), so a single pathological field can
    never crowd out the response or overflow the window — without truncating
    ordinary large inputs the way the old flat 40k-char cap did. Recurses through
    dict/list and leaves non-string scalars untouched. Pass ``max_tokens`` to
    override the per-field budget (mainly for tests).
    """
    budget = input_token_budget(model) if max_tokens is None else max_tokens
    return _compact(value, model=model, max_tokens=budget)


def _compact(value: Any, *, model: str, max_tokens: int) -> Any:
    if isinstance(value, str):
        return _truncate_to_tokens(value, model=model, max_tokens=max_tokens)
    if isinstance(value, dict):
        return {
            key: _compact(item, model=model, max_tokens=max_tokens)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_compact(item, model=model, max_tokens=max_tokens) for item in value]
    return value


def _truncate_to_tokens(text: str, *, model: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    tokens = count_text_tokens(text, model=model)
    if tokens <= max_tokens:
        return text
    # Proportional character slice (with a small margin) keyed on the token
    # ratio. Exact token slicing would need the model's real tokenizer, which is
    # unnecessary for a guard whose budget already dwarfs real inputs.
    keep = max(1, int(len(text) * max_tokens / tokens * 0.97))
    _LOG.warning(
        "compact_input truncated a ~%d-token string to ~%d tokens (model=%s)",
        tokens,
        max_tokens,
        model,
    )
    return text[:keep] + _TRUNCATION_SUFFIX


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
