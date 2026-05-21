from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import Citation, VersionedModel


class ConceptCandidate(VersionedModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    source_chapter_id: str
    owner_task_id: str


class ConceptReconciledItem(VersionedModel):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    source_chapter_ids: list[str] = Field(default_factory=list)


class ConceptResult(VersionedModel):
    name: str
    body_md: str
    related: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    owner_task_id: str


class ConceptReconcileResult(VersionedModel):
    concepts: list[ConceptReconciledItem] = Field(default_factory=list)
    alias_map: dict[str, str] = Field(default_factory=dict)
