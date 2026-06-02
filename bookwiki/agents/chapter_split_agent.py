from __future__ import annotations

import os
from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import ChapterSplitAuditResult, ChapterSplitResult
from bookwiki.split.chapter_splitter import SplitResult
from bookwiki.split.chapter_splitter import split_sources_by_structure


class ChapterSplitAgent:
    kind: ClassVar[str] = "chapter_split_llm_v1"
    output_model: ClassVar[type[ChapterSplitResult]] = ChapterSplitResult
    model_key: ClassVar[str] = "split"
    prompt_name: ClassVar[str] = "chapter_split"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""You are the chapter-split audit agent.

Review the deterministic source split using compact metadata only.
Write report_md as a concise audit note explaining source coverage, unassigned fragments,
chapter/source_ref distribution, and any risk.
Do not request, reproduce, or summarize full chapter source text.""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> ChapterSplitResult:
        result = split_sources_by_structure(
            list(inp.get("source_paths", [])), str(inp.get("approved_structure", ""))
        )
        deterministic = ChapterSplitResult(
            chapters=result.chapters,
            chapter_titles=result.chapter_titles,
            alignment=result.alignment,
            coverage=result.coverage,
            report_md=result.report_md,
        )
        if _uses_bookwiki_runtime(runtime) and not _env_flag("BOOKWIKI_SPLIT_USE_LLM", default=True):
            return deterministic
        audit = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ChapterSplitAuditResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=_audit_input(inp, result),
            draft=ChapterSplitAuditResult(report_md=result.report_md),
        )
        audited = ChapterSplitAuditResult.model_validate(audit)
        return ChapterSplitResult(
            chapters=deterministic.chapters,
            chapter_titles=deterministic.chapter_titles,
            alignment=deterministic.alignment,
            coverage=deterministic.coverage,
            report_md=audited.report_md or deterministic.report_md,
        )


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _uses_bookwiki_runtime(runtime: LLMRuntime) -> bool:
    return runtime.__class__.__module__.startswith("bookwiki.scheduler.llm")


def _audit_input(inp: dict[str, Any], result: SplitResult) -> dict[str, Any]:
    source_refs_by_chapter: dict[str, list[str]] = {}
    fragment_counts: dict[str, int] = {}
    char_counts: dict[str, int] = {}
    reason_counts: dict[str, dict[str, int]] = {}

    for item in result.alignment:
        chapter_id = str(item.get("chapter_id") or "")
        if not chapter_id:
            continue
        source_refs_by_chapter.setdefault(chapter_id, []).append(str(item.get("source_ref") or ""))
        fragment_counts[chapter_id] = fragment_counts.get(chapter_id, 0) + 1
        try:
            chars = int(item.get("chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        char_counts[chapter_id] = char_counts.get(chapter_id, 0) + chars
        reason = str(item.get("reason") or "unknown")
        chapter_reasons = reason_counts.setdefault(chapter_id, {})
        chapter_reasons[reason] = chapter_reasons.get(reason, 0) + 1

    chapters = {}
    for chapter_id, title in result.chapter_titles.items():
        chapters[chapter_id] = {
            "title": title,
            "fragment_count": fragment_counts.get(chapter_id, 0),
            "char_count": char_counts.get(chapter_id, 0),
            "assignment_reasons": reason_counts.get(chapter_id, {}),
            "source_refs": source_refs_by_chapter.get(chapter_id, []),
        }

    return {
        "source_paths": inp.get("source_paths", []),
        "source_hashes": inp.get("source_hashes", []),
        "approved_structure": inp.get("approved_structure", ""),
        "book_notes": inp.get("book_notes", ""),
        "coverage": result.coverage,
        "chapters": chapters,
        "unassigned_source_refs": source_refs_by_chapter.get("appendix", []),
    }
