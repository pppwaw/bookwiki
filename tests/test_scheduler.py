from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from bookwiki.pipeline.nodes import generate_node
from bookwiki.scheduler.cache import run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import LLMRuntime, TestLLMRuntime


class EchoResult(BaseModel):
    value: str


class EchoAgent:
    kind: ClassVar[str] = "echo_agent_v1"
    output_model: ClassVar[type[EchoResult]] = EchoResult
    calls: ClassVar[int] = 0

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> EchoResult:
        self.__class__.calls += 1
        return EchoResult(value=f"{inp['value']}:{model}")


@pytest.mark.asyncio
async def test_generate_node_fans_out_over_all_chapter_sources(tmp_path: Path) -> None:
    book_dir = tmp_path / "book"
    for chapter_id, title in {
        "chapter-1": "Search",
        "chapter-2": "Heuristics",
    }.items():
        source_path = book_dir / "work" / "chapter_sources" / chapter_id / "source.md"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            f"# {title}\n\n<!-- source_ref: {chapter_id}-p001 -->\n\n{title} content.",
            encoding="utf-8",
        )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "chapter_sources": {
            "chapter-1": "work/chapter_sources/chapter-1/source.md",
            "chapter-2": "work/chapter_sources/chapter-2/source.md",
        },
        "chapter_titles": {"chapter-1": "Search", "chapter-2": "Heuristics"},
    }

    first = await generate_node(state, cfg)
    second = await generate_node(state, cfg)

    assert set(first["agent_results"]) == {"chapter-1", "chapter-2"}
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    for chapter_id, outputs in second["agent_results"].items():
        assert set(outputs) == {"chapter", "summary", "quiz", "card"}
        for rel_path in outputs.values():
            assert (book_dir / rel_path).exists(), chapter_id


@pytest.mark.asyncio
async def test_run_with_cache_reports_cache_miss_then_hit(tmp_path: Path) -> None:
    EchoAgent.calls = 0

    first = await run_with_cache(
        EchoAgent,
        {"value": "one"},
        model="stub",
        cache_dir=tmp_path / ".cache",
        runtime=TestLLMRuntime(),
    )
    second = await run_with_cache(
        EchoAgent,
        {"value": "one"},
        model="stub",
        cache_dir=tmp_path / ".cache",
        runtime=TestLLMRuntime(),
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.key == second.key
    assert first.result == second.result == EchoResult(value="one:stub")
    assert EchoAgent.calls == 1
