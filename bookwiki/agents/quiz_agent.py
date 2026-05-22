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
from bookwiki.schemas.quiz import QuizItem, QuizResult


class QuizAgent:
    kind: ClassVar[str] = "quiz_llm_v1"
    output_model: ClassVar[type[QuizResult]] = QuizResult
    model_key: ClassVar[str] = "quiz"
    prompt_name: ClassVar[str] = "quiz"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        version="v1",
        body="""You are the quiz-generation agent.

Create multiple-choice questions that test understanding, not trivia.
Create exactly the requested quiz_per_chapter number of questions when provided.
Each question must have at least two plausible choices and exactly one answer matching
one of the choices.
Explanations should teach why the answer is correct.
Use citations from the chapter source for each item.
Avoid trick questions, ambiguous wording, and answers that require outside knowledge.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> QuizResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        refs = source_refs(inp)
        count = _requested_count(inp, "quiz_per_chapter", "quizPerChapter", 1)
        items = [
            QuizItem(
                question=f"What is central idea {index + 1} in {title}?",
                choices=[title, "Unrelated topic"],
                answer=title,
                explanation="The answer should be grounded in the chapter source.",
                citations=[citation(inp)],
            )
            for index in range(count)
        ]
        draft = QuizResult(chapter_id=ch_id, items=items, owner_task_id=f"{ch_id}:quiz")
        llm_input = _content_input(inp, refs)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=QuizResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=llm_input,
            draft=draft,
            allowed_citation_refs=refs,
        )
        return QuizResult.model_validate(result)


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
