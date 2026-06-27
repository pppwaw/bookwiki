from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import ValidationError

from bookwiki.agents.document import model_to_document


class RecordingRuntime:
    def __init__(
        self,
        responses: list[dict[str, Any] | BaseModel | str],
        tool_calls: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.responses = responses
        self.tool_calls = list(tool_calls or [])
        self.calls: list[dict[str, Any]] = []
        self.tool_results: list[Any] = []

    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
        max_tokens: int | None = None,
    ) -> BaseModel:
        last_error: ValidationError | None = None
        for _attempt in range(max_retries):
            self.calls.append(
                {
                    "model": model,
                    "output_model": output_model,
                    "system": system,
                    "user": user,
                    "context": context,
                    "image_paths": [str(path) for path in image_paths or []],
                    "max_retries": max_retries,
                    "max_tokens": max_tokens,
                }
            )
            response = self.responses.pop(0)
            try:
                if isinstance(response, output_model):
                    return output_model.model_validate(
                        response.model_dump(mode="json"), context=context
                    )
                return output_model.model_validate(response, context=context)
            except ValidationError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        msg = "recording runtime exhausted without a response"
        raise ValueError(msg)

    async def generate_document(
        self,
        *,
        model: str,
        system: str,
        user: str,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "image_paths": [str(path) for path in image_paths or []],
                "max_retries": max_retries,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, str):
            return response
        if isinstance(response, BaseModel):
            body_field = _document_body_field(response.model_dump())
            return model_to_document(response, body_field=body_field)
        if isinstance(response, dict):
            return _dict_to_document(response)
        msg = "recording runtime document response must be text or document-like data"
        raise TypeError(msg)

    async def generate_with_tools(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        tools: Sequence[dict[str, Any]],
        tool_executor: Any,
        context: dict[str, Any] | None = None,
        image_paths: Sequence[str | Path] | None = None,
        max_tool_rounds: int = 4,
        max_retries: int = 2,
    ) -> BaseModel:
        self.calls.append(
            {
                "model": model,
                "output_model": output_model,
                "system": system,
                "user": user,
                "tools": list(tools),
                "context": context,
                "max_tool_rounds": max_tool_rounds,
            }
        )
        pending = self.tool_calls
        self.tool_calls = []
        for name, args in pending:
            result = tool_executor(name, args)
            if inspect.isawaitable(result):
                result = await result
            self.tool_results.append(result)
        response = self.responses.pop(0)
        if isinstance(response, output_model):
            return output_model.model_validate(response.model_dump(mode="json"), context=context)
        return output_model.model_validate(response, context=context)


def _dict_to_document(response: dict[str, Any]) -> str:
    body_field = _document_body_field(response)
    body = response.get(body_field, "")
    frontmatter = {key: value for key, value in response.items() if key != body_field}
    import yaml

    frontmatter_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter_text}\n---\n{body}"


def _document_body_field(response: dict[str, Any]) -> str:
    if "body_md" in response:
        return "body_md"
    if "summary_md" in response:
        return "summary_md"
    msg = "document-like data must contain body_md or summary_md"
    raise ValueError(msg)
