from __future__ import annotations

from collections.abc import Iterable

from bookwiki.schemas.quiz import ExamResult
from bookwiki.schemas.report import Issue


def check_exam(result: ExamResult, known_source_refs: Iterable[str]) -> list[Issue]:
    """Validate an exam paper against the book's real source refs.

    Per-type structure (answer in options, fill_blank blanks, worked rubric) is already
    enforced by the ``ExamQuestion`` schema, so the checker covers the one thing the schema
    cannot know about: whether every ``source_refs`` entry points at a real source. Issues
    carry the exam's ``owner_task_id`` (e.g. ``chXX:exam`` / ``chXX:explain``) so the repair
    loop can route them.
    """

    known = set(known_source_refs)
    issues: list[Issue] = []
    for question in result.questions:
        unknown = [ref for ref in question.source_refs if ref not in known]
        if unknown:
            issues.append(
                Issue(
                    severity="error",
                    code="EXAM_UNKNOWN_SOURCE_REF",
                    message=(
                        f"exam question {question.id!r} cites source_refs that do not exist: "
                        f"{unknown}"
                    ),
                    owner_task_id=result.owner_task_id,
                )
            )
    return issues
