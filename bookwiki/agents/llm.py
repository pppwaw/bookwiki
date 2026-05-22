from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

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
    max_attempts: int = 3,
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
    user = prompt.user
    for attempt in range(1, max_attempts + 1):
        result = await runtime.generate(
            model=model,
            output_model=output_model,
            system=prompt.system,
            user=user,
        )
        invalid_refs = _invalid_citation_refs(result, allowed_refs)
        if not invalid_refs:
            return result
        if attempt == max_attempts:
            raise ValueError(_citation_error_message(invalid_refs, allowed_refs))
        user = f"{prompt.user}\n\n{_citation_retry_instruction(invalid_refs, allowed_refs)}"
    raise RuntimeError("unreachable citation retry state")


def compact_input(value: Any, *, max_chars: int = 40_000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "\n\n[truncated]"
    if isinstance(value, dict):
        return {key: compact_input(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_input(item, max_chars=max_chars) for item in value]
    return value


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
