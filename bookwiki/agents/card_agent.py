from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.schemas.card import CardItem, CardResult


class CardAgent:
    kind: ClassVar[str] = "card"
    output_model: ClassVar[type[CardResult]] = CardResult
    model_key: ClassVar[str] = "card"

    async def run(self, inp: dict[str, Any], *, model: str) -> CardResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        card = CardItem(
            front=f"{title}: M1 output",
            back="A valid Pydantic object used to render vault Markdown and SQLite rows.",
            citations=[citation(inp)],
        )
        return CardResult(chapter_id=ch_id, items=[card], owner_task_id=f"{ch_id}:card")
