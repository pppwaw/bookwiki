from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

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


class RubricPoint(VersionedModel):
    point: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)


class WorkedItem(VersionedModel):
    question: str
    reference_answer: str = Field(
        min_length=1,
        description="Complete worked solution or proof shown after evaluation.",
    )
    rubric: list[RubricPoint] = Field(
        min_length=1,
        description="Weighted grading points checked against the learner's worked answer.",
    )
    explanation: str = ""
    citations: list[Citation] = Field(default_factory=list)
    slot_id: str = Field(
        default="",
        description="Canonical id of the inline <QuizItemSlot/> this worked item fills.",
    )


class QuizResult(VersionedModel):
    chapter_id: str
    items: list[QuizItem] = Field(default_factory=list)
    worked_items: list[WorkedItem] = Field(default_factory=list)
    owner_task_id: str


# --- Chapter exam + paper walkthrough --------------------------------------
#
# Exam questions are a discriminated union (by ``type``) so one mixed paper can
# carry choice / fill-blank / worked items together. This is a NEW representation
# used only by the exam page + paper walkthrough; the legacy ``QuizItem`` /
# ``WorkedItem`` above keep driving the inline knowledge/application quizzes.


class _ExamQuestionBase(VersionedModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    explanation: str = ""
    concepts: list[str] = Field(default_factory=list)
    # True when the item is rewritten/aligned from a past exam paper.
    from_exam: bool = False
    source_refs: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    # Foldable "concept refresh" shown between question and full solution in the
    # paper walkthrough. Empty for generated chapter exams.
    concept_recap_md: str = ""


class ChoiceQuestion(_ExamQuestionBase):
    type: Literal["single_choice", "multiple_choice"]
    options: list[str] = Field(min_length=2)
    # Single choice carries exactly one entry; multiple choice may carry several.
    answer: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_answer(self) -> ChoiceQuestion:
        if self.type == "single_choice" and len(self.answer) != 1:
            msg = "single_choice answer must carry exactly one option"
            raise ValueError(msg)
        options = set(self.options)
        missing = [choice for choice in self.answer if choice not in options]
        if missing:
            msg = f"answer entries not among options: {missing}"
            raise ValueError(msg)
        return self


class FillBlankQuestion(_ExamQuestionBase):
    type: Literal["fill_blank"]
    # One group of accepted answers per blank (normalised before comparison).
    accepted_answers: list[list[str]] = Field(min_length=1)

    @field_validator("accepted_answers")
    @classmethod
    def _each_blank_non_empty(cls, value: list[list[str]]) -> list[list[str]]:
        for index, group in enumerate(value):
            if not group:
                msg = f"fill_blank accepted_answers[{index}] needs at least one candidate"
                raise ValueError(msg)
        return value


class WorkedQuestion(_ExamQuestionBase):
    type: Literal["worked"]
    reference_answer: str = Field(min_length=1)
    rubric: list[RubricPoint] = Field(min_length=1)


ExamQuestion = Annotated[
    ChoiceQuestion | FillBlankQuestion | WorkedQuestion,
    Field(discriminator="type"),
]


class ExamResult(VersionedModel):
    chapter_id: str
    questions: list[ExamQuestion] = Field(default_factory=list)
    owner_task_id: str
