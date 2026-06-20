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


class QuizPlacement(VersionedModel):
    after_block: int = Field(ge=0)
    item_indexes: list[int] = Field(default_factory=list)
    title: str = "Quiz"


class QuizResult(VersionedModel):
    chapter_id: str
    items: list[QuizItem] = Field(default_factory=list)
    placements: list[QuizPlacement] = Field(default_factory=list)
    owner_task_id: str


class KnowledgeQuizResult(VersionedModel):
    chapter_id: str
    section_index: int
    items: list[QuizItem] = Field(default_factory=list)
    owner_task_id: str
