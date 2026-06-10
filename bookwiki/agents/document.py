from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError


def parse_frontmatter_document(
    text: str,
    *,
    output_model: type[BaseModel],
    body_field: str,
    defaults: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> BaseModel:
    try:
        frontmatter, body = _split_frontmatter(text)
        loaded = yaml.safe_load(frontmatter) if frontmatter.strip() else {}
    except yaml.YAMLError as exc:
        msg = f"invalid YAML frontmatter for {output_model.__name__}: {exc}"
        raise ValueError(msg) from exc
    except ValueError as exc:
        msg = f"invalid frontmatter document for {output_model.__name__}: {exc}"
        raise ValueError(msg) from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        msg = f"frontmatter for {output_model.__name__} must be a mapping"
        raise ValueError(msg)

    data = dict(loaded)
    data[body_field] = body.strip()
    data.update(defaults)
    try:
        return output_model.model_validate(data, context=context)
    except ValidationError as exc:
        msg = f"document validation failed for {output_model.__name__}: {exc}"
        raise ValueError(msg) from exc


def model_to_document(model: BaseModel, *, body_field: str) -> str:
    data = model.model_dump(mode="json")
    body = data.pop(body_field, "")
    frontmatter = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n{body}"


def _split_frontmatter(text: str) -> tuple[str, str]:
    stripped = _strip_mdx_fence(text)
    match = re.match(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError("expected a leading YAML frontmatter block delimited by ---")
    return match.group(1), match.group(2)


def _strip_mdx_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(
        r"\A```(?:mdx|markdown|md)?\s*\n(.*?)\n```\s*\Z",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else stripped
