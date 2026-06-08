"""Tests for section figure supplementation (Phase 4, form A).

``supplement_section_figures`` runs after a section is finalised: it turns the
section's ``figure_requests`` into generated/reused figures and returns a
``figure_id -> <BookFigure/> tag`` registry plus best-effort warning issues.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bookwiki.generate.sections import supplement_section_figures
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from bookwiki.schemas.section import FigureRequest, SectionResult
from tests.fakes import RecordingRuntime

PLOT_CODE = (
    "import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.plot([0, 1, 2], [0, 1, 4])\n"
)


def _section(figure_requests: list[FigureRequest]) -> SectionResult:
    body = "Intro paragraph.\n\n" + "\n\n".join(
        f'<BookFigure id="{request.figure_ref}" />' for request in figure_requests
    )
    return SectionResult(
        chapter_id="chapter-1",
        section_index=0,
        title="S0",
        body_md=body,
        concepts=[],
        citations=[],
        figure_requests=figure_requests,
        owner_task_id="chapter-1:section:000",
    )


def _image_result(figure_ref: str, *, ok: bool = True, caption: str = "Demo") -> dict[str, object]:
    return {
        "chapter_id": "chapter-1",
        "section_index": 0,
        "figure_ref": figure_ref,
        "ok": ok,
        "caption": caption,
        "error": "",
        "owner_task_id": "chapter-1:section:000:figure",
    }


def _cfg(book_dir: Path, runtime: object) -> BookConfig:
    return BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)


@pytest.mark.asyncio
async def test_supplement_plot_generates_registers_and_writes_asset(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    section = _section([FigureRequest(kind="plot", figure_ref="ch1-s0-demo", rationale="a line")])
    runtime = RecordingRuntime(
        [_image_result("ch1-s0-demo", caption="A demo line")],
        tool_calls=[("run_plot", {"code": PLOT_CODE})],
    )
    cfg = _cfg(tmp_path / "book", runtime)

    registry, issues = await supplement_section_figures(
        cfg=cfg, chapter_id="chapter-1", section=section, source_figures=[]
    )

    assert issues == []
    assert "ch1-s0-demo" in registry
    tag = registry["ch1-s0-demo"]
    assert 'id="ch1-s0-demo"' in tag
    assert "/bookwiki-assets/generated/chapter-1/ch1-s0-demo.png" in tag
    assert "A demo line" in tag
    asset = cfg.book_dir / "work" / "assets" / "generated" / "chapter-1" / "ch1-s0-demo.png"
    assert asset.exists()
    assert runtime.tool_results and runtime.tool_results[0]["ok"] is True


@pytest.mark.asyncio
async def test_supplement_plot_failure_records_warning_not_raise(tmp_path: Path) -> None:
    # Forbidden code is rejected by run_plot's AST guard -> ok=false -> warning.
    section = _section([FigureRequest(kind="plot", figure_ref="ch1-s0-x", rationale="x")])
    runtime = RecordingRuntime(
        [_image_result("ch1-s0-x")],
        tool_calls=[("run_plot", {"code": "import socket\n"})],
    )
    cfg = _cfg(tmp_path / "book", runtime)

    registry, issues = await supplement_section_figures(
        cfg=cfg, chapter_id="chapter-1", section=section, source_figures=[]
    )

    assert registry == {}
    assert len(issues) == 1
    assert issues[0].code == "FIGURE_SUPPLEMENT_FAILED"
    assert issues[0].severity == "warning"
    assert issues[0].owner_task_id == "chapter-1:chapter"


@pytest.mark.asyncio
async def test_supplement_reuse_existing_known_figure_is_noop(tmp_path: Path) -> None:
    section = _section(
        [FigureRequest(kind="reuse_existing", figure_ref="paper-p001-b001", rationale="reuse")]
    )
    cfg = _cfg(tmp_path / "book", RecordingRuntime([]))

    registry, issues = await supplement_section_figures(
        cfg=cfg,
        chapter_id="chapter-1",
        section=section,
        source_figures=[{"id": "paper-p001-b001", "caption": "Tree"}],
    )

    # The source id is already in the chapter figure index; no new entry needed.
    assert registry == {}
    assert issues == []


@pytest.mark.asyncio
async def test_supplement_reuse_unknown_figure_records_warning(tmp_path: Path) -> None:
    section = _section([FigureRequest(kind="reuse_existing", figure_ref="ghost", rationale="x")])
    cfg = _cfg(tmp_path / "book", RecordingRuntime([]))

    registry, issues = await supplement_section_figures(
        cfg=cfg, chapter_id="chapter-1", section=section, source_figures=[]
    )

    assert registry == {}
    assert len(issues) == 1
    assert issues[0].code == "FIGURE_SUPPLEMENT_FAILED"


@pytest.mark.asyncio
async def test_supplement_noop_when_no_requests(tmp_path: Path) -> None:
    section = _section([])
    cfg = _cfg(tmp_path / "book", TestLLMRuntime())

    registry, issues = await supplement_section_figures(
        cfg=cfg, chapter_id="chapter-1", section=section, source_figures=[]
    )

    assert registry == {}
    assert issues == []
