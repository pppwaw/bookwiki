from __future__ import annotations

from pathlib import Path

import pytest

import bookwiki.pipeline.nodes as nodes
from bookwiki.agents.llm import generate_document_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.generate.sections import ChapterGenerationResult
from bookwiki.pipeline.nodes import generate_node
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.card import CardResult
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.quiz import QuizResult
from bookwiki.schemas.summary import SummaryResult

_FAILED_MARKER = "THIS IS NOT A VALID FRONTMATTER DOCUMENT zzz"


class _DocRuntime:
    """Records user prompts; returns a queued document text per call."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.user_prompts: list[str] = []

    async def generate_document(
        self, *, model: str, system: str, user: str, image_paths=None, max_retries: int = 2
    ) -> str:
        self.user_prompts.append(user)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_generate_document_retry_includes_failed_output() -> None:
    runtime = _DocRuntime(
        [
            _FAILED_MARKER,
            (
                "---\nchapter_id: chapter-1\ntitle: T\n"
                "owner_task_id: chapter-1:chapter\n---\nGood body."
            ),
        ]
    )
    draft = ChapterResult(
        chapter_id="chapter-1", title="T", body_md="draft", owner_task_id="chapter-1:chapter"
    )

    result = await generate_document_with_llm(
        runtime=runtime,
        model="deepseek-v4-pro",
        output_model=ChapterResult,
        agent_name="SectionAgent",
        prompt_name="section",
        prompt_template=PromptTemplate(body="write a section"),
        inp={"language": "zh-CN"},
        draft=draft,
        body_field="body_md",
        defaults={},
    )

    assert result.body_md == "Good body."
    assert len(runtime.user_prompts) == 2
    # The retry prompt must include the prior failed document and a fix-in-place cue.
    assert _FAILED_MARKER in runtime.user_prompts[1]
    assert "定点修正" in runtime.user_prompts[1]
    # And it must NOT snowball: the failed marker appears once (rebuilt from base prompt).
    assert runtime.user_prompts[1].count(_FAILED_MARKER) == 1


def _gen_result(ch_id: str) -> ChapterGenerationResult:
    return ChapterGenerationResult(
        chapter=ChapterResult(
            chapter_id=ch_id, title=ch_id, body_md="# body", owner_task_id=f"{ch_id}:chapter"
        ),
        quiz=QuizResult(chapter_id=ch_id, items=[], owner_task_id=f"{ch_id}:quiz"),
        card=CardResult(chapter_id=ch_id, items=[], owner_task_id=f"{ch_id}:card"),
        summary=SummaryResult(
            chapter_id=ch_id, summary_md="s", owner_task_id=f"{ch_id}:summary"
        ),
        issues=[],
        generated_figures={},
        cache_hit=False,
    )


@pytest.mark.asyncio
async def test_generate_node_writes_survivors_and_raises_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book_dir = tmp_path / "book"
    chapters = ["chapter-1", "chapter-2", "chapter-3"]
    for ch in chapters:
        source_path = book_dir / "work" / "chapter_sources" / ch / "source.md"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            f"# {ch}\n\n<!-- source_ref: {ch}-p001 -->\n\n{ch} body.", encoding="utf-8"
        )
    cfg = BookConfig(
        book_dir=book_dir, book_id="book", title="Book", llm_runtime=TestLLMRuntime()
    )
    state = {
        "chapter_sources": {ch: f"work/chapter_sources/{ch}/source.md" for ch in chapters},
        "chapter_titles": {ch: ch for ch in chapters},
    }

    async def fake_generate_chapter_sections(
        *, chapter_id: str, **_kwargs
    ) -> ChapterGenerationResult:
        if chapter_id == "chapter-2":
            raise RuntimeError("boom in chapter-2")
        return _gen_result(chapter_id)

    monkeypatch.setattr(nodes, "generate_chapter_sections", fake_generate_chapter_sections)

    with pytest.raises(RuntimeError, match="chapter-2"):
        await generate_node(state, cfg)

    # Survivors were still written before the loud failure.
    result_dir = book_dir / "work" / "agent_results"
    assert (result_dir / "chapter-1.chapter.json").exists()
    assert (result_dir / "chapter-3.chapter.json").exists()
    assert not (result_dir / "chapter-2.chapter.json").exists()
