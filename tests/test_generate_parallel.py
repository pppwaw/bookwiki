"""Tests for chapter-level parallel generation (Phase 5 / M4).

``generate_node`` fans chapters out with ``asyncio.gather`` bounded by
``cfg.chapter_concurrency`` (chapter-level parallel, section-level serial).
``asyncio.gather`` preserves input order, so artifacts stay deterministic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bookwiki.pipeline.nodes import generate_node
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime


class ConcurrencyProbeRuntime:
    """Draft-echoing runtime that records the peak number of concurrent calls.

    Each chapter runs its LLM calls serially, so the peak number of in-flight
    ``generate`` calls equals the number of chapters running concurrently.
    """

    def __init__(self, delay: float = 0.02) -> None:
        self._inner = TestLLMRuntime()
        self.delay = delay
        self.current = 0
        self.max_concurrent = 0

    async def generate(self, **kwargs: Any) -> Any:
        return await self._record_call(self._inner.generate, **kwargs)

    async def generate_document(self, **kwargs: Any) -> str:
        return await self._record_call(self._inner.generate_document, **kwargs)

    async def _record_call(self, call: Any, **kwargs: Any) -> Any:
        self.current += 1
        self.max_concurrent = max(self.max_concurrent, self.current)
        try:
            await asyncio.sleep(self.delay)
            return await call(**kwargs)
        finally:
            self.current -= 1

    async def generate_with_tools(self, **kwargs: Any) -> Any:
        return await self._inner.generate_with_tools(**kwargs)


def _write_chapters(book_dir: Path, count: int) -> dict[str, str]:
    sources: dict[str, str] = {}
    for index in range(1, count + 1):
        ch_id = f"chapter-{index}"
        rel = f"work/chapter_sources/{ch_id}/source.md"
        path = book_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Topic {index}\n\n<!-- source_ref: src-p{index:03d} -->\n\nBody {index}.",
            encoding="utf-8",
        )
        sources[ch_id] = rel
    return sources


@pytest.mark.asyncio
async def test_generate_node_parallel_produces_all_chapters(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    sources = _write_chapters(book_dir, 3)
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
        generation={"quizPerChapter": 1, "cardsPerChapter": 1},
    )
    state = {
        "chapter_sources": sources,
        "chapter_titles": {ch: ch for ch in sources},
        "chapter_topics": {ch: [f"t{ch}"] for ch in sources},
    }

    result = await generate_node(state, cfg)

    assert set(result["agent_results"]) == {"chapter-1", "chapter-2", "chapter-3"}
    for ch_id, outputs in result["agent_results"].items():
        assert set(outputs) == {"chapter", "summary", "quiz", "card"}
        payload = json.loads((book_dir / outputs["chapter"]).read_text(encoding="utf-8"))
        assert payload["result"]["chapter_id"] == ch_id
        assert payload["result"]["owner_task_id"] == f"{ch_id}:chapter"


@pytest.mark.asyncio
async def test_generate_node_respects_chapter_concurrency_cap(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    sources = _write_chapters(book_dir, 6)
    runtime = ConcurrencyProbeRuntime(delay=0.02)
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=runtime,
        generation={
            "quizPerChapter": 1,
            "cardsPerChapter": 1,
            "maxChapterConcurrency": 2,
        },
    )
    state = {
        "chapter_sources": sources,
        "chapter_titles": {ch: ch for ch in sources},
        "chapter_topics": {ch: [f"t{ch}"] for ch in sources},
    }

    await generate_node(state, cfg)

    assert cfg.chapter_concurrency == 2
    # The cap is respected, and parallelism actually happened (not serial).
    assert runtime.max_concurrent <= 2
    assert runtime.max_concurrent == 2


@pytest.mark.asyncio
async def test_generate_node_single_chapter_runs_without_parallelism(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    sources = _write_chapters(book_dir, 1)
    runtime = ConcurrencyProbeRuntime(delay=0.0)
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=runtime,
        generation={"quizPerChapter": 1, "cardsPerChapter": 1},
    )
    state = {
        "chapter_sources": sources,
        "chapter_titles": {"chapter-1": "chapter-1"},
        "chapter_topics": {"chapter-1": ["t"]},
    }

    result = await generate_node(state, cfg)

    assert set(result["agent_results"]) == {"chapter-1"}
    assert runtime.max_concurrent == 1
