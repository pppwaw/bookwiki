from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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
    ) -> BaseModel:
        self.calls.append(
            {
                "model": model,
                "output_model": output_model,
                "system": system,
                "user": user,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, output_model):
            return response
        return output_model.model_validate(response)
