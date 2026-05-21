from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import Citation, VersionedModel


class CardItem(VersionedModel):
    front: str
    back: str
    citations: list[Citation] = Field(default_factory=list)


class CardResult(VersionedModel):
    chapter_id: str
    items: list[CardItem] = Field(default_factory=list)
    owner_task_id: str
