from __future__ import annotations

from bookwiki.schemas.card import CardResult
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import VersionedModel
from bookwiki.schemas.quiz import QuizResult


class LessonResult(VersionedModel):
    chapter_id: str
    chapter: ChapterResult
    quiz: QuizResult
    card: CardResult
    owner_task_id: str
