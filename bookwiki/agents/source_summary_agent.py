from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from bookwiki.schemas.source import SourceSummaryResult


class SourceSummaryAgent:
    kind: ClassVar[str] = "source_summary"
    output_model: ClassVar[type[SourceSummaryResult]] = SourceSummaryResult
    model_key: ClassVar[str] = "summary"

    async def run(self, inp: str | Path, *, model: str) -> SourceSummaryResult:
        path = Path(inp)
        source_id = path.stem
        return SourceSummaryResult(
            source_id=source_id,
            summary_md=f"Stub summary for {path.name}.",
            source_refs=[f"{source_id}-p001"],
        )
