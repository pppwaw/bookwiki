from __future__ import annotations

from bookwiki.schemas.card import CardResult
from bookwiki.schemas.common import VersionedModel
from bookwiki.schemas.quiz import QuizResult


class QuizCardResult(VersionedModel):
    """Chapter-level practice bundle produced after the chapter body is assembled.

    ``QuizCardAgent`` reads the fully assembled chapter body (so quiz placement
    ``after_block`` indices line up with the rendered blocks) and emits the quiz
    and recall cards together in one structured call. The pipeline still writes
    the ``quiz`` and ``card`` artifacts separately, so downstream stages
    (``check``/``integrate``) stay byte-compatible.
    """

    chapter_id: str
    quiz: QuizResult
    card: CardResult
    owner_task_id: str
