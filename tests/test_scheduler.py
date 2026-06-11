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


def test_config_hash_unaffected_by_runtime_injection(tmp_path: Path) -> None:
    from bookwiki.scheduler.resume import config_hash

    cfg = BookConfig(book_dir=tmp_path / "book", book_id="book", title="Book")
    before = config_hash(cfg)
    cfg.llm_runtime = TestLLMRuntime()
    after = config_hash(cfg)

    assert before == after


@pytest.mark.asyncio
async def test_run_with_cache_regenerates_on_corrupt_entry(tmp_path: Path) -> None:
    from bookwiki.scheduler.cache import task_key

    EchoAgent.calls = 0
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(parents=True)
    key = task_key(EchoAgent, {"value": "one"}, model="stub")
    # Pre-seed a half-written / corrupt cache entry.
    (cache_dir / f"{key}.json").write_text("{ this is not valid json", encoding="utf-8")

    result = await run_with_cache(
        EchoAgent,
        {"value": "one"},
        model="stub",
        cache_dir=cache_dir,
        runtime=TestLLMRuntime(),
    )

    # Corrupt entry is ignored and regenerated (cache miss, agent ran once).
    assert result.cache_hit is False
    assert EchoAgent.calls == 1
    assert result.result == EchoResult(value="one:stub")


def test_task_key_changes_when_output_schema_changes() -> None:
    from bookwiki.scheduler.cache import task_key

    class _AgentA:
        kind = "schema_probe_v1"

        class output_model(BaseModel):  # noqa: N801 - mimic ClassVar output_model
            value: str

    class _AgentB:
        kind = "schema_probe_v1"

        class output_model(BaseModel):  # noqa: N801
            value: str
            extra: int

    key_a = task_key(_AgentA, {"x": 1}, model="stub")
    key_b = task_key(_AgentB, {"x": 1}, model="stub")

    assert key_a != key_b  # added field invalidates the cache key
