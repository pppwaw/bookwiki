from __future__ import annotations

from bookwiki.schemas.card import CardResult
from bookwiki.schemas.common import VersionedModel
from bookwiki.schemas.quiz import QuizResult


class QuizCardResult(VersionedModel):
    """Legacy chapter-level practice bundle kept for old cached payloads."""

    chapter_id: str
    quiz: QuizResult
    card: CardResult
    owner_task_id: str
