from __future__ import annotations

from pydantic import Field, computed_field

from bookwiki.schemas.common import VersionedModel


class Issue(VersionedModel):
    severity: str
    code: str
    message: str
    owner_task_id: str


class CheckReport(VersionedModel):
    status: str
    issues: list[Issue] = Field(default_factory=list)

    @computed_field
    @property
    def repair_targets(self) -> list[str]:
        seen: set[str] = set()
        targets: list[str] = []
        for issue in self.issues:
            if issue.severity in {"error", "critical"} and issue.owner_task_id not in seen:
                seen.add(issue.owner_task_id)
                targets.append(issue.owner_task_id)
        return targets
