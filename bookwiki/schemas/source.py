from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import VersionedModel


class SourceSummaryResult(VersionedModel):
    source_id: str
    summary_md: str
    source_refs: list[str] = Field(default_factory=list)
    detected_chapter_id: str | None = None
    detected_title: str | None = None


class StructureResult(VersionedModel):
    proposed_structure_md: str
    chapters: list[str] = Field(default_factory=list)


class ChapterSplitResult(VersionedModel):
    chapters: dict[str, str] = Field(default_factory=dict)
    chapter_titles: dict[str, str] = Field(default_factory=dict)
    alignment: list[dict[str, object]] = Field(default_factory=list)
    coverage: dict[str, float | int] = Field(default_factory=dict)
    report_md: str


class RepairResult(VersionedModel):
    owner_task_id: str
    action: str
    notes: str
