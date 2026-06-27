from __future__ import annotations

from bookwiki.generate.exam_pool import UNMAPPED, build_exam_pools
from bookwiki.schemas.source import SourceSummaryResult

CHAPTER_CONCEPTS = {
    "ch-search": ["state space", "frontier", "A*"],
    "ch-logic": ["resolution", "CNF"],
}


def _exam_summary(*questions: dict[str, object]) -> SourceSummaryResult:
    return SourceSummaryResult(
        source_id="midterm",
        summary_md="Mid-term paper.",
        is_exam=True,
        exam_questions=list(questions),  # type: ignore[arg-type]
    )


def test_maps_question_to_chapter_by_concept_overlap() -> None:
    summary = _exam_summary(
        {"question": "Define A* frontier.", "concepts": ["A*"], "source_refs": ["m-p001"]}
    )

    pools = build_exam_pools([summary], CHAPTER_CONCEPTS)

    assert [q.question for q in pools["ch-search"]] == ["Define A* frontier."]
    assert "ch-logic" not in pools


def test_unmatched_question_goes_to_unmapped_pool() -> None:
    summary = _exam_summary(
        {"question": "Unrelated trivia.", "concepts": ["thermodynamics"], "source_refs": []}
    )

    pools = build_exam_pools([summary], CHAPTER_CONCEPTS)

    assert [q.question for q in pools[UNMAPPED]] == ["Unrelated trivia."]


def test_non_exam_summaries_are_ignored() -> None:
    non_exam = SourceSummaryResult(source_id="slides", summary_md="Lecture slides.")

    pools = build_exam_pools([non_exam], CHAPTER_CONCEPTS)

    assert pools == {}


def test_question_maps_to_best_overlap_chapter_only() -> None:
    summary = _exam_summary(
        {
            "question": "Resolution refutation in CNF.",
            "concepts": ["CNF", "resolution"],
            "source_refs": ["m-p002"],
        }
    )

    pools = build_exam_pools([summary], CHAPTER_CONCEPTS)

    assert "ch-logic" in pools
    assert "ch-search" not in pools
