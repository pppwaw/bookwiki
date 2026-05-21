from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.schemas.summary import SummaryResult


class SummaryAgent:
    kind: ClassVar[str] = "summary"
    output_model: ClassVar[type[SummaryResult]] = SummaryResult
    model_key: ClassVar[str] = "summary"

    async def run(self, inp: dict[str, Any], *, model: str) -> SummaryResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        return SummaryResult(
            chapter_id=ch_id,
            summary_md=f"{title} introduces the core ideas available in the source bundle.",
            key_points=["Trace source material", "Generate deterministic study content"],
            citations=[citation(inp)],
            owner_task_id=f"{ch_id}:summary",
        )
