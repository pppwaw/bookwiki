from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from bookwiki.convert.source_normalizer import (
    SourceBlock,
    _render_figure,
)
from bookwiki.scheduler.cache import CacheResult
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.utils.files import read_json
from bookwiki.utils.hashing import sha256_text
from bookwiki.utils.logging import get_logger

State = dict[str, Any]

# Every stage module shares this single logger (named after the historical
# ``nodes`` module) so log output stays under one namespace and tests/log files
# can filter on ``bookwiki.pipeline.nodes`` regardless of which stage emitted.
_LOG = get_logger("bookwiki.pipeline.nodes")

APPROVED_STRUCTURE_MARKER = "# bookwiki: approved-structure"
PENDING_STRUCTURE_MARKER = "# bookwiki: pending-structure-review"

_fanout_semaphores: dict[tuple[int, str], asyncio.Semaphore] = {}


def log_progress(stage: str, index: int, total: int, message: str, *args: Any) -> None:
    """Emit a uniform ``<stage>: [i/N] message`` progress line.

    Keeps per-item loop logging consistent and scannable across stages so the
    reader can always see "where are we" without the lines drowning each other.
    """
    _LOG.info("%s: [%d/%d] " + message, stage, index, total, *args)


def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _json_model(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)


def _agent_result_payload(agent_cls: type[Any], model: str, result: Any) -> dict[str, Any]:
    payload = _json_model(result)
    return {
        "_schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "_agent": agent_cls.__name__,
        "_model": model,
        "result": payload,
    }


def _agent_result(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result")
    return result if isinstance(result, dict) else data


def _cache_dir(cfg: BookConfig) -> Path:
    return cfg.cache_dir / "tasks"


def _stage_cache_hit(results: list[CacheResult]) -> bool:
    return bool(results) and all(item.cache_hit for item in results)


def _safe_file_stem(value: str, *, fallback_prefix: str = "item") -> str:
    normalized = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-.")
    if normalized:
        return normalized
    return f"{fallback_prefix}-{sha256_text(value)[:8]}"


def _clear_generated_files(directory: Path, pattern: str) -> None:
    for path in directory.glob(pattern):
        if path.is_file():
            path.unlink()


def _citation_items(citations: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"ref_id": str(item.get("ref_id", "")), "quote": str(item.get("quote", ""))}
        for item in citations
    ]


def _display_chapter_title(chapter_id: str, title: str) -> str:
    # The chapter title is the verbatim free-form name; the id is just a slug derived from it.
    # No "Chapter N" prefix is synthesised — a title that wants one already contains it.
    return str(title).strip() or str(chapter_id)


def _mdx_link_exists(base_dir: Path, target: str) -> bool:
    clean = target.split("#", 1)[0]
    if not clean:
        return True
    path = (base_dir / clean).resolve()
    candidates = [path]
    if path.suffix == "":
        candidates.extend([path.with_suffix(".mdx"), path / "index.mdx"])
    return any(candidate.exists() for candidate in candidates)


def _replace_book_figure(markdown: str, block: dict[str, Any]) -> tuple[str, bool]:
    figure = _render_figure(_source_block_from_manifest(block), "")
    if not figure:
        return markdown, False
    pattern = _book_figure_pattern(str(block.get("block_id") or ""))
    if not pattern.search(markdown):
        return markdown, False
    return pattern.sub(lambda _match: figure, markdown, count=1), True


def _book_figure_pattern(block_id: str) -> re.Pattern[str]:
    escaped = re.escape(block_id)
    return re.compile(rf'<BookFigure\b(?=[^>]*\bid="{escaped}")[^>]*/>')


def _source_block_from_manifest(block: dict[str, Any]) -> SourceBlock:
    return SourceBlock(
        block_id=str(block.get("block_id") or ""),
        page_ref=str(block.get("page_ref") or ""),
        page_idx=_int_setting(block.get("page_idx"), 0),
        block_index=_int_setting(block.get("block_index"), 0),
        type=str(block.get("type") or "image"),
        text=str(block.get("text_preview") or ""),
        bbox=block.get("bbox") if isinstance(block.get("bbox"), list) else None,
        asset_path=str(block.get("asset_path") or "") or None,
        caption=str(block.get("caption") or "") or None,
    )


def _int_setting(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _load_skeleton(state: State, cfg: BookConfig) -> dict[str, Any] | None:
    """Load the skeleton produced by ``build_skeleton_node`` if present.

    Returns the inner ``BookSkeleton`` payload (without the ``_agent`` wrapper),
    or ``None`` when the state has no ``skeleton`` key (e.g. old runs that
    pre-date M2 or partial reruns).
    """
    rel_path = state.get("skeleton")
    if not rel_path:
        return None
    payload = read_json(cfg.book_dir / rel_path, default={})
    return _agent_result(payload) or None


# Filename of a chapter's exam page. Exam pages are *structural* (a folder's ``index.mdx`` holds the
# teaching body, ``exam.mdx`` holds the exam) and legitimately carry no QuizBlock/Anki/Sources, so
# ``check_node`` keys off this name to exempt them from those pedagogical-section checks.
_EXAM_PAGE_FILENAME = "exam.mdx"

__all__ = [
    "State",
    "_LOG",
    "log_progress",
    "APPROVED_STRUCTURE_MARKER",
    "PENDING_STRUCTURE_MARKER",
    "_fanout_semaphores",
    "_rel",
    "_json_model",
    "_agent_result_payload",
    "_agent_result",
    "_cache_dir",
    "_stage_cache_hit",
    "_safe_file_stem",
    "_clear_generated_files",
    "_citation_items",
    "_display_chapter_title",
    "_mdx_link_exists",
    "_replace_book_figure",
    "_book_figure_pattern",
    "_source_block_from_manifest",
    "_int_setting",
    "_load_skeleton",
    "_EXAM_PAGE_FILENAME",
]
