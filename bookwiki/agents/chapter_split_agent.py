from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.source import ChapterSplitResult
from bookwiki.split.chapter_splitter import split_sources_by_structure


class ChapterSplitAgent:
    kind: ClassVar[str] = "chapter_split"
    output_model: ClassVar[type[ChapterSplitResult]] = ChapterSplitResult
    model_key: ClassVar[str] = "split"

    async def run(self, inp: dict[str, Any], *, model: str) -> ChapterSplitResult:
        result = split_sources_by_structure(
            list(inp.get("source_paths", [])), str(inp.get("approved_structure", ""))
        )
        return ChapterSplitResult(
            chapters=result.chapters,
            chapter_titles=result.chapter_titles,
            alignment=result.alignment,
            coverage=result.coverage,
            report_md=result.report_md,
        )
