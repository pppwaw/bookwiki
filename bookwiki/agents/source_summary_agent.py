from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown
from bookwiki.schemas.source import SourceSummaryResult


class SourceSummaryAgent:
    kind: ClassVar[str] = "source_summary"
    output_model: ClassVar[type[SourceSummaryResult]] = SourceSummaryResult
    model_key: ClassVar[str] = "summary"

    async def run(self, inp: str | Path | dict[str, str], *, model: str) -> SourceSummaryResult:
        path = Path(inp["path"] if isinstance(inp, dict) else inp)
        source_id = path.stem
        body = path.read_text(encoding="utf-8", errors="ignore")
        source_refs = SOURCE_REF_RE.findall(body)
        summary_lines = [
            cleaned
            for line in clean_markdown(SOURCE_REF_RE.sub("", body)).splitlines()
            if (cleaned := _clean_summary_line(line))
        ][:5]
        summary = " ".join(summary_lines)[:600] or f"No extractable text in {path.name}."
        return SourceSummaryResult(
            source_id=source_id,
            summary_md=f"Summary for {source_id}: {summary}",
            source_refs=source_refs or [f"{source_id}-text"],
        )


def _clean_summary_line(line: str) -> str:
    return line.lstrip("#").lstrip("-").strip()
