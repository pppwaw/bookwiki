from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.card import CardItem, CardResult


class CardAgent:
    kind: ClassVar[str] = "card_llm_v1"
    output_model: ClassVar[type[CardResult]] = CardResult
    model_key: ClassVar[str] = "card"
    prompt_name: ClassVar[str] = "card"

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> CardResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        card = CardItem(
            front=title,
            back="A source-grounded recall card for this chapter.",
            citations=[citation(inp)],
        )
        draft = CardResult(chapter_id=ch_id, items=[card], owner_task_id=f"{ch_id}:card")
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=CardResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            inp=inp,
            draft=draft,
        )
        return CardResult.model_validate(result)
