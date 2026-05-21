from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import Citation, VersionedModel


class ChapterResult(VersionedModel):
    chapter_id: str
    title: str
    body_md: str
    concepts: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    owner_task_id: str
