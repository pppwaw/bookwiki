from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import (
    chapter_document,
    chapter_id,
    chapter_title,
    citation,
    source_refs,
)
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.card import CardItem, CardResult


class CardAgent:
    kind: ClassVar[str] = "card_llm_v1"
    output_model: ClassVar[type[CardResult]] = CardResult
    model_key: ClassVar[str] = "card"
    prompt_name: ClassVar[str] = "card"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""You are the flashcard-generation agent.

Create concise recall cards for the chapter.
Create exactly the requested cards_per_chapter number of cards when provided.
The front should be a question, term, or prompt that is easy to review.
The back should be short, precise, and source-grounded.
Prefer high-value concepts, definitions, formula meanings, and common confusions.
Avoid cards that merely repeat a chapter title or ask vague questions.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> CardResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        refs = source_refs(inp)
        count = _requested_count(inp, "cards_per_chapter", "cardsPerChapter", 1)
        cards = [
            CardItem(
                front=f"{title} review prompt {index + 1}",
                back="A source-grounded recall card for this chapter.",
                citations=[citation(inp)],
            )
            for index in range(count)
        ]
        draft = CardResult(chapter_id=ch_id, items=cards, owner_task_id=f"{ch_id}:card")
        llm_input = _content_input(inp, refs)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=CardResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=llm_input,
            draft=draft,
            allowed_citation_refs=refs,
        )
        return CardResult.model_validate(result)


def _content_input(inp: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    payload = {key: value for key, value in inp.items() if key != "source_md"}
    payload["document_xml"] = chapter_document(inp)
    payload["allowed_source_refs"] = sorted(refs)
    return payload


def _requested_count(inp: dict[str, Any], snake_key: str, camel_key: str, default: int) -> int:
    try:
        value = int(inp.get(snake_key, inp.get(camel_key, default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
