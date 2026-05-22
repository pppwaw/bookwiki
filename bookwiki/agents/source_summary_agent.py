from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown
from bookwiki.schemas.source import SourceSummaryResult


class SourceSummaryAgent:
    kind: ClassVar[str] = "source_summary_v2"
    output_model: ClassVar[type[SourceSummaryResult]] = SourceSummaryResult
    model_key: ClassVar[str] = "summary"

    async def run(self, inp: str | Path | dict[str, str], *, model: str) -> SourceSummaryResult:
        path = Path(inp["path"] if isinstance(inp, dict) else inp)
        source_id = path.stem
        body = path.read_text(encoding="utf-8", errors="ignore")
        source_refs = SOURCE_REF_RE.findall(body)
        detected_chapter_id, detected_title = _detect_chapter_heading(body)
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
            detected_chapter_id=detected_chapter_id,
            detected_title=detected_title,
        )


def _clean_summary_line(line: str) -> str:
    return line.lstrip("#").lstrip("-").strip()


def _detect_chapter_heading(text: str) -> tuple[str | None, str | None]:
    for line in text.splitlines():
        heading = line.strip().lstrip("#").strip()
        match = re.match(
            r"chapter\s+(\d+)\s+(.+)$",
            heading,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        chapter_id = f"ch{int(match.group(1)):02d}"
        title = _normalize_chapter_title(match.group(2))
        return chapter_id, title
    return None, None


def _normalize_chapter_title(raw: str) -> str:
    title = re.sub(r"\([^)]*\)", "", raw)
    title = re.sub(r"\s+", " ", title).strip(" -:：")
    title = title.replace("The ", "", 1) if title.lower().startswith("the ") else title
    return title.title() if title.islower() else title
