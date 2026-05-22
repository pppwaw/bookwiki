from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

from pydantic import BaseModel

PROMPT_PACKAGE = "bookwiki.agents.prompts"


@dataclass(frozen=True)
class RenderedPrompt:
    system: str
    user: str
    version: str
    cache_key: str


def render_prompt(
    *,
    prompt_name: str,
    agent_name: str,
    inp: Any,
    draft: BaseModel | dict[str, Any],
    output_model: type[BaseModel] | None = None,
) -> RenderedPrompt:
    common = _load_template("common_system")
    user_template = _load_template("user")
    agent = _load_template(prompt_name)
    prompt_version = f"{common.version}+{user_template.version}+{agent.version}"
    output_name = output_model.__name__ if output_model is not None else "PydanticModel"
    user = user_template.body.format_map(
        {
            "agent_name": agent_name,
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "output_model": output_name,
            "agent_instructions": agent.body,
            "input_json": _json(inp),
            "draft_json": _json(draft),
        }
    )
    cache_key = _hash_parts(common.raw, user_template.raw, agent.raw)
    return RenderedPrompt(
        system=common.body,
        user=user,
        version=prompt_version,
        cache_key=cache_key,
    )


def prompt_cache_key(prompt_name: str | None) -> str:
    if not prompt_name:
        return ""
    common = _load_template("common_system")
    user_template = _load_template("user")
    agent = _load_template(prompt_name)
    return _hash_parts(common.raw, user_template.raw, agent.raw)


@dataclass(frozen=True)
class _Template:
    version: str
    body: str
    raw: str


def _load_template(name: str) -> _Template:
    raw = resources.files(PROMPT_PACKAGE).joinpath(f"{name}.md").read_text(encoding="utf-8")
    header, sep, body = raw.partition("---")
    if not sep:
        msg = f"prompt template {name!r} is missing metadata separator"
        raise ValueError(msg)
    version = "v0"
    for line in header.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "version":
            version = value.strip()
            break
    return _Template(version=version, body=body.strip(), raw=raw)


def _json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _hash_parts(*parts: str) -> str:
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return digest
