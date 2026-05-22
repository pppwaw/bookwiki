from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.agents.llm import generate_with_llm
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.quiz import QuizItem, QuizResult


class QuizAgent:
    kind: ClassVar[str] = "quiz_llm_v1"
    output_model: ClassVar[type[QuizResult]] = QuizResult
    model_key: ClassVar[str] = "quiz"
    prompt_name: ClassVar[str] = "quiz"

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> QuizResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        item = QuizItem(
            question=f"What is a central idea in {title}?",
            choices=[title, "Unrelated topic"],
            answer=title,
            explanation="The answer should be grounded in the chapter source.",
            citations=[citation(inp)],
        )
        draft = QuizResult(chapter_id=ch_id, items=[item], owner_task_id=f"{ch_id}:quiz")
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=QuizResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            inp=inp,
            draft=draft,
        )
        return QuizResult.model_validate(result)
