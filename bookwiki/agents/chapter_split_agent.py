from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.source import ChapterSplitResult


class ChapterSplitAgent:
    kind: ClassVar[str] = "chapter_split"
    output_model: ClassVar[type[ChapterSplitResult]] = ChapterSplitResult
    model_key: ClassVar[str] = "split"

    async def run(self, inp: dict[str, Any], *, model: str) -> ChapterSplitResult:
        source_md = str(inp.get("source_md", ""))
        midpoint = max(1, len(source_md) // 2)
        report = (
            "# Chapter Split Report\n\n"
            "- ch01: stub first segment\n"
            "- ch02: stub second segment\n"
        )
        return ChapterSplitResult(
            chapters={"ch01": source_md[:midpoint], "ch02": source_md[midpoint:] or source_md},
            report_md=report,
        )
