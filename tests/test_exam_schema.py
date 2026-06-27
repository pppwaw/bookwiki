from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.quiz import ExamQuestion

_ADAPTER: TypeAdapter[ExamQuestion] = TypeAdapter(ExamQuestion)


def test_discriminator_routes_single_choice() -> None:
    question = _ADAPTER.validate_python(
        {
            "type": "single_choice",
            "id": "ex-1",
            "question": "What does A* minimise?",
            "options": ["f(n)", "g(n) only", "h(n) only"],
            "answer": ["f(n)"],
            "explanation": "A* expands by f = g + h.",
            "source_refs": ["source-p001"],
        }
    )

    assert question.type == "single_choice"
    assert question.answer == ["f(n)"]
    assert question.from_exam is False
    assert question.schema_version == SCHEMA_VERSION


def test_single_choice_rejects_multiple_answers() -> None:
    with pytest.raises(ValidationError, match="single_choice"):
        _ADAPTER.validate_python(
            {
                "type": "single_choice",
                "id": "ex-1",
                "question": "Pick one.",
                "options": ["a", "b", "c"],
                "answer": ["a", "b"],
                "source_refs": ["source-p001"],
            }
        )


def test_choice_answer_must_be_within_options() -> None:
    with pytest.raises(ValidationError, match="not among"):
        _ADAPTER.validate_python(
            {
                "type": "multiple_choice",
                "id": "ex-2",
                "question": "Pick all correct.",
                "options": ["a", "b", "c"],
                "answer": ["a", "z"],
                "source_refs": ["source-p001"],
            }
        )


def test_fill_blank_rejects_empty_blank_group() -> None:
    with pytest.raises(ValidationError, match="accepted"):
        _ADAPTER.validate_python(
            {
                "type": "fill_blank",
                "id": "ex-3",
                "question": "A* uses f(n) = ___ + ___.",
                "accepted_answers": [["g(n)"], []],
                "source_refs": ["source-p001"],
            }
        )
