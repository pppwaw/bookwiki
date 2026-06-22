from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import Citation, VersionedModel


class QuizItem(VersionedModel):
    question: str
    choices: list[str] = Field(min_length=2)
    answer: str
    explanation: str
    citations: list[Citation] = Field(default_factory=list)
    figure_ref: str = Field(
        default="",
        description=(
            "Optional id of an existing chapter <BookFigure> the question depends on; "
            "the figure is shown under the question. Empty when no figure is needed."
        ),
    )
    slot_id: str = Field(
        default="",
        description=(
            "Canonical id of the inline <QuizItemSlot/> this item fills (application "
            "quizzes). Assigned by the system, never by the model. Empty for knowledge "
            "quizzes, which are authored inline by SectionAgent and need no slot."
        ),
    )


class QuizResult(VersionedModel):
    chapter_id: str
    items: list[QuizItem] = Field(default_factory=list)
    owner_task_id: str
