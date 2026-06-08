from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import ValidationError


class RecordingRuntime:
    def __init__(
        self,
        responses: list[dict[str, Any] | BaseModel],
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
