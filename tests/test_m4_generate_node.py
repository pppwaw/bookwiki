from __future__ import annotations

import json

import pytest

from bookwiki.pipeline.nodes import generate_node
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime


@pytest.mark.asyncio
async def test_generate_node_writes_only_m4_content_agent_outputs(tmp_path) -> None:
    book_dir = tmp_path / "book"
    source_path = book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "# Search\n\n<!-- source_ref: source-p001 -->\n\nState space search.",
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
        generation={"quizPerChapter": 2, "cardsPerChapter": 3},
    )
    state = {
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "chapter_titles": {"chapter-1": "Search"},
    }

    result = await generate_node(state, cfg)

    outputs = result["agent_results"]["chapter-1"]
    assert set(outputs) == {"chapter", "summary", "quiz", "card"}
    for kind, rel_path in outputs.items():
        payload = json.loads((book_dir / rel_path).read_text(encoding="utf-8"))
        assert payload["_schema_version"] == "llm.v1"
        assert payload["_prompt_version"].startswith("v1+")
        assert payload["_agent"].endswith("Agent")
        assert payload["result"]["owner_task_id"].endswith(f":{kind}")
