from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.source import RepairResult


class ReviewAgent:
    kind: ClassVar[str] = "review"
    output_model: ClassVar[type[RepairResult]] = RepairResult
    model_key: ClassVar[str] = "review"

    async def run(self, inp: dict[str, Any], *, model: str) -> RepairResult:
        owner = str(inp.get("owner_task_id", "unknown:review"))
        return RepairResult(
            owner_task_id=owner,
            action="noop",
            notes="M1 stub repair recorded the issue without changing generated content.",
        )
