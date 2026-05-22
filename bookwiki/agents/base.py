from __future__ import annotations

from typing import ClassVar, Protocol

from pydantic import BaseModel

from bookwiki.scheduler.llm import LLMRuntime


class Agent[InputT, OutputT: BaseModel](Protocol):
    kind: ClassVar[str]
    output_model: ClassVar[type[OutputT]]
    model_key: ClassVar[str]

    async def run(self, inp: InputT, *, model: str, runtime: LLMRuntime) -> OutputT:
        """Return a validated Pydantic result."""
