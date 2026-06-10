from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bookwiki.agents.document import model_to_document, parse_frontmatter_document
from bookwiki.agents.prompting import PromptTemplate, render_prompt
from bookwiki.scheduler.llm import LLMRuntime


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
    user = _document_user_prompt(prompt.user, draft_document=draft_document)
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
            user = (
                f"{user}\n\n"
                "上一次 MDX-direct 文档无法解析或校验失败。\n"
                f"错误：{exc}\n"
                "请修正后重新返回完整文档：开头必须是 YAML frontmatter，"
                "随后是 `---` 与 raw MDX body。"
            )
    assert last_error is not None
    raise last_error


def compact_input(value: Any, *, max_chars: int = 40_000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "\n\n[truncated]"
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


def _invalid_citation_refs(result: BaseModel, allowed_refs: set[str]) -> list[str]:
    if not allowed_refs:
        return []
    seen = set()
    invalid = []
    for ref_id in _iter_citation_ref_ids(result.model_dump(mode="json")):
        if ref_id not in allowed_refs and ref_id not in seen:
            seen.add(ref_id)
            invalid.append(ref_id)
    return invalid


def _iter_citation_ref_ids(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        if "ref_id" in value and "quote" in value:
            yield str(value["ref_id"])
        for item in value.values():
            yield from _iter_citation_ref_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_citation_ref_ids(item)


def _citation_retry_instruction(invalid_refs: list[str], allowed_refs: set[str]) -> str:
    return (
        "Citation validation failed.\n"
        f"Invalid ref_id values: {', '.join(invalid_refs)}\n"
        f"Allowed source_ref values: {', '.join(sorted(allowed_refs))}\n"
        "Return corrected JSON only. Do not invent source_ref values."
    )


def _citation_error_message(invalid_refs: list[str], allowed_refs: set[str]) -> str:
    return (
        f"invalid citation ref_id values: {', '.join(invalid_refs)}; "
        f"allowed source_ref values: {', '.join(sorted(allowed_refs))}"
    )
