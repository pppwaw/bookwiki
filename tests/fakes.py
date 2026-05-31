from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import ValidationError


class RecordingRuntime:
    def __init__(self, responses: list[dict[str, Any] | BaseModel]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

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
