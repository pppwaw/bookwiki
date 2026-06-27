from __future__ import annotations

from bookwiki.checkers.exam_checker import check_exam
from bookwiki.schemas.quiz import ExamResult


def _result(**question: object) -> ExamResult:
    base = {
        "type": "single_choice",
        "id": "ex-1",
        "question": "What does A* minimise?",
        "options": ["f(n)", "g(n)"],
        "answer": ["f(n)"],
        "source_refs": ["source-p001"],
    }
    base.update(question)
    return ExamResult(
        chapter_id="ch01",
        owner_task_id="ch01:exam",
        questions=[base],  # type: ignore[list-item]
    )


def test_clean_exam_has_no_issues() -> None:
    result = _result()

    issues = check_exam(result, known_source_refs={"source-p001"})

    assert issues == []


def test_flags_unknown_source_ref() -> None:
    result = _result(source_refs=["source-p001", "source-p999"])

    issues = check_exam(result, known_source_refs={"source-p001"})

    assert len(issues) == 1
    assert issues[0].code == "EXAM_UNKNOWN_SOURCE_REF"
    assert issues[0].severity == "error"
    assert issues[0].owner_task_id == "ch01:exam"
    assert "source-p999" in issues[0].message
