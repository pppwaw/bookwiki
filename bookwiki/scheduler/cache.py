from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from bookwiki.agents.prompting import prompt_cache_key
from bookwiki.scheduler.llm import LLMRuntime, build_runtime
from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)


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
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _output_schema_hash(agent_cls: type[Any]) -> str:
    """Hash the agent's output schema so a field add/rename invalidates stale entries.

    The previous key captured only the (validated) inputs, so a schema change with an
    unchanged class name could surface a stale cached payload. Including the JSON schema
    digest makes such changes a cache miss. NOTE: this invalidates all pre-existing cache
    entries on first deploy (a one-time, intended cost).
    """
    output_model = getattr(agent_cls, "output_model", None)
    if output_model is None or not hasattr(output_model, "model_json_schema"):
        return ""
    schema = json.dumps(output_model.model_json_schema(), sort_keys=True)
    return hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]


def task_key(agent_cls: type[Any], *inputs: Any, model: str) -> str:
    payload = {
        "agent": f"{agent_cls.__module__}.{agent_cls.__name__}",
        "kind": getattr(agent_cls, "kind", agent_cls.__name__),
        "model": model,
        "prompt": prompt_cache_key(getattr(agent_cls, "prompt_template", None)),
        "output_schema": _output_schema_hash(agent_cls),
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
    agent_name = agent_cls.__name__

    if output_path.exists() and not force:
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            cached = output_model.model_validate(payload["result"])
        except (json.JSONDecodeError, KeyError, ValidationError, OSError) as exc:
            # A half-written / schema-stale entry must not crash the run: drop it and
            # regenerate as a normal cache miss.
            LOGGER.warning("corrupt cache entry %s; regenerating: %s", output_path, exc)
            output_path.unlink(missing_ok=True)
        else:
            LOGGER.info(
                "agent cache_hit agent=%s model=%s key=%s path=%s",
                agent_name,
                model,
                key,
                output_path,
            )
            return CacheResult(cached, True, key, output_path)

    inp = inputs[0] if len(inputs) == 1 else list(inputs)
    if runtime is not None:
        llm_runtime = runtime
    else:
        LOGGER.warning(
            "no shared runtime injected for agent=%s; building ad-hoc runtime", agent_name
        )
        llm_runtime = build_runtime()
    LOGGER.info("agent start agent=%s model=%s key=%s", agent_name, model, key)
    try:
        result = await agent_cls().run(inp, model=model, runtime=llm_runtime)
    except Exception:
        LOGGER.exception("agent error agent=%s model=%s key=%s", agent_name, model, key)
        raise
    # Atomic write: serialise to a temp file then os.replace, so an interrupted process
    # never leaves a half-written entry the next run would choke on.
    tmp_path = output_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(
            {
                "key": key,
                "agent": agent_name,
                "model": model,
                "result": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp_path, output_path)
    LOGGER.info(
        "agent done agent=%s model=%s key=%s path=%s",
        agent_name,
        model,
        key,
        output_path,
    )
    return CacheResult(result, False, key, output_path)
