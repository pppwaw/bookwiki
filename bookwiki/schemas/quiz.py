from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import Citation, VersionedModel


class QuizItem(VersionedModel):
    question: str
    choices: list[str] = Field(min_length=2)
    answer: str
    explanation: str
    citations: list[Citation] = Field(default_factory=list)


class QuizResult(VersionedModel):
    chapter_id: str
    items: list[QuizItem] = Field(default_factory=list)
    owner_task_id: str
