from __future__ import annotations

import json
from html import escape
from typing import Literal

from bookwiki.schemas.quiz import (
    ChoiceQuestion,
    ExamQuestion,
    ExamResult,
    FillBlankQuestion,
    WorkedQuestion,
)

ExamMode = Literal["exam", "walkthrough"]


def render_exam_mdx(result: ExamResult, *, mode: ExamMode = "exam") -> str:
    """Render an :class:`ExamResult` to the plaintext ``<ExamBlock>`` MDX grammar.

    The same grammar backs both the generated chapter exam (``mode="exam"``) and the
    past-paper walkthrough (``mode="walkthrough"``, which also emits ``<ExamConceptRecap>``).
    Answers/accepted-answers/rubric ride as JSON props; question / choices / reference /
    explanation / recap ride as children so multiline LaTeX stays unescaped.
    """

    items = "\n\n".join(_render_item(question, mode=mode) for question in result.questions)
    chapter = escape(result.chapter_id, quote=True)
    return f'<ExamBlock chapterId="{chapter}" mode="{mode}">\n{items}\n</ExamBlock>\n'


def _render_item(question: ExamQuestion, *, mode: ExamMode) -> str:
    attrs = [f'id="{escape(question.id, quote=True)}"', f'type="{question.type}"']
    body: list[str] = [_tag("ExamQuestion", question.question)]

    if isinstance(question, ChoiceQuestion):
        attrs.append(f"answer={{{_json(_answer_ids(question))}}}")
        body.append(_render_choices(question))
    elif isinstance(question, FillBlankQuestion):
        attrs.append(f"acceptedAnswers={{{_json(question.accepted_answers)}}}")
    elif isinstance(question, WorkedQuestion):
        rubric = [{"point": point.point, "weight": point.weight} for point in question.rubric]
        attrs.append(f"referenceAnswer={{{_json(question.reference_answer)}}}")
        attrs.append(f"rubric={{{_json(rubric)}}}")

    if mode == "walkthrough" and question.concept_recap_md.strip():
        body.append(_tag("ExamConceptRecap", question.concept_recap_md))

    if question.explanation.strip():
        body.append(_tag("ExamExplanation", question.explanation))

    if question.from_exam:
        attrs.append("fromExam")

    opening = f"<ExamItem {' '.join(attrs)}>"
    return "\n".join([opening, *body, "</ExamItem>"])


def _render_choices(question: ChoiceQuestion) -> str:
    rows = "\n".join(
        f'<ExamChoice id="choice-{index}">\n{option.strip()}\n</ExamChoice>'
        for index, option in enumerate(question.options, start=1)
    )
    return f"<ExamChoices>\n{rows}\n</ExamChoices>"


def _answer_ids(question: ChoiceQuestion) -> list[str]:
    by_text = {option: f"choice-{index}" for index, option in enumerate(question.options, start=1)}
    return [by_text[answer] for answer in question.answer]


def _tag(name: str, text: str) -> str:
    return f"<{name}>\n{text.strip()}\n</{name}>"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
