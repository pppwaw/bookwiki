from __future__ import annotations

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
) -> BaseModel:
    prompt = render_prompt(
        prompt_name=prompt_name,
        prompt_template=prompt_template,
        agent_name=agent_name,
        inp=compact_input(inp),
        draft=draft,
        output_model=output_model,
    )
    return await runtime.generate(
        model=model,
        output_model=output_model,
        system=prompt.system,
        user=prompt.user,
    )


def compact_input(value: Any, *, max_chars: int = 40_000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "\n\n[truncated]"
    if isinstance(value, dict):
        return {key: compact_input(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_input(item, max_chars=max_chars) for item in value]
    return value
