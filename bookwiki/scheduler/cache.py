from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bookwiki.agents.prompting import prompt_cache_key
from bookwiki.scheduler.llm import LLMRuntime, build_runtime


@dataclass(frozen=True)
class CacheResult:
    result: BaseModel
    cache_hit: bool
    key: str
    path: Path


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def task_key(agent_cls: type[Any], *inputs: Any, model: str) -> str:
    payload = {
        "agent": f"{agent_cls.__module__}.{agent_cls.__name__}",
        "kind": getattr(agent_cls, "kind", agent_cls.__name__),
        "model": model,
        "prompt": prompt_cache_key(getattr(agent_cls, "prompt_name", None)),
        "inputs": _jsonable(inputs),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{getattr(agent_cls, 'kind', agent_cls.__name__)}-{digest}"


async def run_with_cache(
    agent_cls: type[Any],
    *inputs: Any,
    model: str,
    cache_dir: str | Path = "work/.cache/tasks",
    force: bool = False,
    runtime: LLMRuntime | None = None,
) -> CacheResult:
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    key = task_key(agent_cls, *inputs, model=model)
    output_path = cache_path / f"{key}.json"
    output_model = agent_cls.output_model

    if output_path.exists() and not force:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        return CacheResult(output_model.model_validate(payload["result"]), True, key, output_path)

    inp = inputs[0] if len(inputs) == 1 else list(inputs)
    llm_runtime = runtime if runtime is not None else build_runtime()
    result = await agent_cls().run(inp, model=model, runtime=llm_runtime)
    output_path.write_text(
        json.dumps(
            {
                "key": key,
                "agent": agent_cls.__name__,
                "model": model,
                "result": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return CacheResult(result, False, key, output_path)
