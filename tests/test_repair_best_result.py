from __future__ import annotations

from pathlib import Path

import pytest

import bookwiki.generate.sections as sections
from bookwiki.generate.sections import (
    _body_too_short,
    _validate_chapter_artifact_inline,
)
from bookwiki.generate.validate_artifact import ArtifactIssue
from bookwiki.scheduler.cache import CacheResult
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.chapter import ChapterResult


def _chapter(body: str) -> ChapterResult:
    return ChapterResult(
        chapter_id="chapter-1",
        title="T",
        body_md=body,
        concepts=[],
        citations=[],
        owner_task_id="chapter-1:chapter",
    )


def _cache_result(chapter: ChapterResult) -> CacheResult:
    return CacheResult(result=chapter, cache_hit=False, key="k", path=Path("x"))


def test_body_too_short_predicate() -> None:
    prev = "x" * 100
    assert _body_too_short("x" * 33, prev) is True  # below 0.34
    assert _body_too_short("x" * 40, prev) is False  # above 0.34
    assert _body_too_short("", "") is False  # empty previous never trips


@pytest.mark.asyncio
async def test_chapter_inline_returns_fewest_issue_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = BookConfig(book_dir=tmp_path / "book", book_id="book", title="Book")
    cfg.generation["maxRepairRounds"] = 2

    original = _chapter("A" * 400)  # 1 issue (best)
    worse1 = _chapter("B" * 400)  # 2 issues
    worse2 = _chapter("C" * 400)  # 3 issues

    # validate_artifact returns a growing number of mdx issues each round.
    issue_sequence = [
        [ArtifactIssue(kind="mdx", message="e1")],
        [ArtifactIssue(kind="mdx", message="e1"), ArtifactIssue(kind="mdx", message="e2")],
        [
            ArtifactIssue(kind="mdx", message="e1"),
            ArtifactIssue(kind="mdx", message="e2"),
            ArtifactIssue(kind="mdx", message="e3"),
        ],
    ]
    calls = {"n": 0}

    async def fake_validate(**_kwargs: object) -> list[ArtifactIssue]:
        result = issue_sequence[min(calls["n"], len(issue_sequence) - 1)]
        calls["n"] += 1
        return result

    repair_candidates = iter([worse1, worse2])

    async def fake_run_with_cache(*_args: object, **_kwargs: object) -> CacheResult:
        return _cache_result(next(repair_candidates))

    monkeypatch.setattr(sections, "validate_artifact", fake_validate)
    monkeypatch.setattr(sections, "run_with_cache", fake_run_with_cache)

    final, _cache, issue = await _validate_chapter_artifact_inline(
        cfg=cfg, base_payload={}, chapter=original, allowed_refs=set()
    )

    # Exhausted: the fewest-issue version (the original, 1 issue) is kept, not worse2.
    assert final.body_md == original.body_md
    assert issue is not None
    assert issue.code == "CHAPTER_VALIDATION_UNRESOLVED"


@pytest.mark.asyncio
async def test_chapter_inline_discards_truncating_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = BookConfig(book_dir=tmp_path / "book", book_id="book", title="Book")
    cfg.generation["maxRepairRounds"] = 2

    original = _chapter("A" * 400)
    # A repair that returns far below MIN_REPAIR_BODY_RATIO of the original
    # (50 / 400 = 0.125 << 0.34): treated as catastrophic truncation and discarded.
    truncated = _chapter("B" * 50)

    async def fake_validate(**_kwargs: object) -> list[ArtifactIssue]:
        return [ArtifactIssue(kind="mdx", message="persistent")]

    repaired_bodies: list[str] = []

    async def fake_run_with_cache(*_args: object, **_kwargs: object) -> CacheResult:
        repaired_bodies.append("called")
        return _cache_result(truncated)

    monkeypatch.setattr(sections, "validate_artifact", fake_validate)
    monkeypatch.setattr(sections, "run_with_cache", fake_run_with_cache)

    final, _cache, issue = await _validate_chapter_artifact_inline(
        cfg=cfg, base_payload={}, chapter=original, allowed_refs=set()
    )

    # The truncating candidate is rejected every round; the original body is kept.
    assert final.body_md == original.body_md
    # Rounds were still consumed (repair was attempted), bounded by maxRepairRounds.
    assert len(repaired_bodies) == 2
    assert issue is not None
