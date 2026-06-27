from __future__ import annotations

from collections.abc import Iterable, Mapping

from bookwiki.schemas.source import DetectedExamQuestion, SourceSummaryResult

# Questions that match no chapter's concepts land here instead of being dropped, so a book
# can still draw on them later (e.g. a whole-book综合卷) without silent loss.
UNMAPPED = "_unmapped"


def build_exam_pools(
    summaries: Iterable[SourceSummaryResult],
    chapter_concepts: Mapping[str, list[str]],
) -> dict[str, list[DetectedExamQuestion]]:
    """Distribute past-exam questions into per-chapter pools by concept overlap.

    Only ``is_exam`` summaries contribute. Each detected question maps to the single chapter
    whose concepts it overlaps most (ties resolve to the first such chapter in
    ``chapter_concepts`` order); a question that overlaps no chapter goes to the :data:`UNMAPPED`
    pool. The result only contains keys that actually received questions.
    """

    normalized = {
        chapter_id: {_norm(concept) for concept in concepts}
        for chapter_id, concepts in chapter_concepts.items()
    }
    pools: dict[str, list[DetectedExamQuestion]] = {}
    for summary in summaries:
        if not summary.is_exam:
            continue
        for question in summary.exam_questions:
            target = _best_chapter(question, normalized)
            pools.setdefault(target, []).append(question)
    return pools


def _best_chapter(
    question: DetectedExamQuestion,
    chapter_concepts: Mapping[str, set[str]],
) -> str:
    question_concepts = {_norm(concept) for concept in question.concepts}
    best_id = UNMAPPED
    best_overlap = 0
    for chapter_id, concepts in chapter_concepts.items():
        overlap = len(question_concepts & concepts)
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = chapter_id
    return best_id


def _norm(concept: str) -> str:
    return concept.strip().casefold()
