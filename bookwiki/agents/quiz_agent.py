from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
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
Each question must have at least two plausible choices and exactly one answer matching
one of the choices.
Explanations should teach why the answer is correct.
Use citations from the chapter source for each item.
Avoid trick questions, ambiguous wording, and answers that require outside knowledge.""",
    )

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
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return QuizResult.model_validate(result)
