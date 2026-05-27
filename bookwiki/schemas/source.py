from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from bookwiki.schemas.common import VersionedModel


class SourceSummaryResult(VersionedModel):
    source_id: str
    summary_md: str
    source_refs: list[str] = Field(default_factory=list)
    detected_chapter_id: str | None = None
    detected_title: str | None = None
    headings: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)


class StructureResult(VersionedModel):
    proposed_structure_yaml: str
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


class SourceLayoutPatch(VersionedModel):
    action: Literal[
        "link_table_parts",
        "attach_caption",
        "promote_heading",
        "demote_repeating_header_footer",
    ]
    source_block_id: str
    target_block_id: str | None = None
    confidence: float = 0.0
    reason: str = ""

    @field_validator("source_block_id", "target_block_id", "reason")
    @classmethod
    def strip_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class SourceLayoutRepairResult(VersionedModel):
    patches: list[SourceLayoutPatch] = Field(default_factory=list)
    notes: str = ""


class VisionCaptionResult(VersionedModel):
    caption_md: str
    key_points: list[str] = Field(default_factory=list)
    source_ref: str
    confidence: float = 0.0
