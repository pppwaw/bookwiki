from __future__ import annotations

from typing import Literal

from pydantic import Field

from bookwiki.schemas.common import VersionedModel


class QualityFinding(VersionedModel):
    category: Literal["language_leak"]
    quote: str
    explanation: str


class QualityReport(VersionedModel):
    owner_task_id: str
    findings: list[QualityFinding] = Field(default_factory=list)
