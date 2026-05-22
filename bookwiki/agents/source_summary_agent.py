from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import SourceSummaryResult


class SourceSummaryAgent:
    kind: ClassVar[str] = "source_summary_llm_v1"
    output_model: ClassVar[type[SourceSummaryResult]] = SourceSummaryResult
    model_key: ClassVar[str] = "summary"

    async def run(
        self, inp: str | Path | dict[str, str], *, model: str, runtime: LLMRuntime
    ) -> SourceSummaryResult:
        path = Path(inp["path"] if isinstance(inp, dict) else inp)
        body = path.read_text(encoding="utf-8", errors="ignore")
        draft = _draft_summary(path, body)
        payload = {
            "path": str(path),
            "source_id": path.stem,
            "source_text": body,
            "sha256": inp.get("sha256") if isinstance(inp, dict) else None,
        }
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SourceSummaryResult,
            agent_name=self.__class__.__name__,
            task=(
                "Summarize this source markdown for later book structuring. Detect chapter "
                "numbers/titles when present, list source_refs exactly, and extract "
                "headings/key terms."
            ),
            inp=payload,
            draft=draft,
        )
        return SourceSummaryResult.model_validate(result)


def _draft_summary(path: Path, body: str) -> SourceSummaryResult:
    source_id = path.stem
    source_refs = SOURCE_REF_RE.findall(body)
    detected_chapter_id, detected_title = _detect_chapter_heading(body)
    headings = _extract_headings(body, source_id)
    cleaned_body = clean_markdown(SOURCE_REF_RE.sub("", body))
    summary_lines = [
        cleaned for line in cleaned_body.splitlines() if (cleaned := _clean_summary_line(line))
    ][:5]
    summary = " ".join(summary_lines)[:600] or f"No extractable text in {path.name}."
    return SourceSummaryResult(
        source_id=source_id,
        summary_md=f"Summary for {source_id}: {summary}",
        source_refs=source_refs or [f"{source_id}-text"],
        detected_chapter_id=detected_chapter_id,
        detected_title=detected_title,
        headings=headings,
        key_terms=_extract_key_terms(cleaned_body, headings),
    )


def _clean_summary_line(line: str) -> str:
    line = line.strip()
    if line.startswith("<!--"):
        return ""
    return re.sub(r"\s+", " ", line.lstrip("#").lstrip("-").strip())


def _extract_headings(text: str, source_id: str) -> list[str]:
    source_title = _normalize_topic(source_id.replace("-", " ").replace("_", " "))
    headings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        heading = _clean_summary_line(stripped)
        if not heading:
            continue
        if _normalize_topic(heading) == source_title:
            continue
        _append_unique(headings, heading)
        if len(headings) >= 8:
            break
    return headings


_SIGNAL_PHRASES = (
    "maximum likelihood estimation",
    "method of maximum likelihood",
    "method of moments",
    "moment estimators",
    "sample moments",
    "population moments",
    "point estimation",
    "parameter estimation",
    "unknown parameters",
    "random sample",
    "sampling distribution",
    "statistic's distribution",
    "population distribution",
    "statistical quantities",
    "joint pmf",
    "joint pdf",
    "exponential distribution",
    "negative binomial distribution",
)


def _extract_key_terms(text: str, headings: list[str]) -> list[str]:
    normalized = _normalize_topic(text)
    terms: list[str] = []
    for phrase in _SIGNAL_PHRASES:
        if phrase in normalized:
            _append_unique(terms, phrase)
    for heading in headings:
        topic = _heading_topic(heading)
        if topic:
            _append_unique(terms, topic)
    return terms[:10]


def _heading_topic(heading: str) -> str:
    topic = re.sub(r"^chapter\s+\d+\s+", "", heading, flags=re.IGNORECASE)
    topic = re.sub(r"\([^)]*\)", "", topic)
    topic = re.sub(r"\s+", " ", topic).strip(" -:")
    if topic.lower().startswith("the "):
        topic = topic[4:]
    if len(topic) < 4:
        return ""
    return topic


def _normalize_topic(text: str) -> str:
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"[-_]+", " ", text)
    return re.sub(r"\s+", " ", text.lower())


def _append_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if not value:
        return
    normalized = _normalize_topic(value)
    if any(_normalize_topic(item) == normalized for item in items):
        return
    items.append(value)


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
