from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.schemas.quiz import QuizItem, QuizResult


class QuizAgent:
    kind: ClassVar[str] = "quiz"
    output_model: ClassVar[type[QuizResult]] = QuizResult
    model_key: ClassVar[str] = "quiz"

    async def run(self, inp: dict[str, Any], *, model: str) -> QuizResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        item = QuizItem(
            question=f"What does {title} demonstrate in the M1 stub pipeline?",
            choices=["A deterministic pipeline", "A live LLM call"],
            answer="A deterministic pipeline",
            explanation="M1 agents are stubs so the pipeline can run without external APIs.",
            citations=[citation(inp)],
        )
        return QuizResult(chapter_id=ch_id, items=[item], owner_task_id=f"{ch_id}:quiz")
