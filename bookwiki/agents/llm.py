from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from bookwiki.scheduler.llm import LLMRuntime


async def generate_with_llm(
    *,
    runtime: LLMRuntime,
    model: str,
    output_model: type[BaseModel],
    agent_name: str,
    task: str,
    inp: Any,
    draft: BaseModel | dict[str, Any],
) -> BaseModel:
    system = (
        "You are a BookWiki agent. Return valid JSON only. "
        f"The JSON must validate against the {output_model.__name__} schema. "
        "Preserve source_ref values exactly and do not invent citations."
    )
    user = (
        f"Agent: {agent_name}\n\n"
        f"Task:\n{task}\n\n"
        f"Input JSON:\n{_json(inp)}\n\n"
        f"Draft JSON:\n{_json(draft)}\n\n"
        "Return only the final JSON object."
    )
    return await runtime.generate(
        model=model,
        output_model=output_model,
        system=system,
        user=user,
    )


def compact_input(value: Any, *, max_chars: int = 40_000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "\n\n[truncated]"
    if isinstance(value, dict):
        return {key: compact_input(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_input(item, max_chars=max_chars) for item in value]
    return value


def _json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(compact_input(value), ensure_ascii=False, indent=2, sort_keys=True)
