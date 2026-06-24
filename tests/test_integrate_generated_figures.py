"""Tests that generated figures survive the integrator (Phase 4).

``_resolve_chapter_figures`` drops any inline ``<BookFigure/>`` whose id is not
in the chapter figure index. Generated figures live only in
``state["generated_figures"]``, so ``_chapter_figure_index`` must merge them or
``run_plot`` output would be silently deleted at render.
"""

from __future__ import annotations

from pathlib import Path

from bookwiki.pipeline.nodes import _chapter_figure_index, _resolve_chapter_figures
from bookwiki.scheduler.config import BookConfig

GENERATED_TAG = (
    '<BookFigure id="ch1-s0-demo" '
    'src="/bookwiki-assets/generated/chapter-1/ch1-s0-demo.png" caption="Demo" />'
)


def _book_with_source(tmp_path: Path) -> BookConfig:
    book_dir = tmp_path / "book"
    source = book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Chapter\n\n<!-- source_ref: s-p001 -->\n\nBody.", encoding="utf-8")
    return BookConfig(book_dir=book_dir, book_id="book", title="Book")


def test_chapter_figure_index_merges_generated_registry(tmp_path: Path) -> None:
    cfg = _book_with_source(tmp_path)
    state = {
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "generated_figures": {"chapter-1": {"ch1-s0-demo": GENERATED_TAG}},
    }

    index = _chapter_figure_index(state, cfg, "chapter-1")

    assert index["ch1-s0-demo"] == GENERATED_TAG


def test_resolve_keeps_registered_generated_figure(tmp_path: Path) -> None:
    cfg = _book_with_source(tmp_path)
    state = {
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "generated_figures": {"chapter-1": {"ch1-s0-demo": GENERATED_TAG}},
    }
    index = _chapter_figure_index(state, cfg, "chapter-1")
    body = 'Intro paragraph.\n\n<BookFigure id="ch1-s0-demo" />\n\nMore.'

    resolved = _resolve_chapter_figures(body, index)

    assert GENERATED_TAG in resolved
    assert 'src="/bookwiki-assets/generated/chapter-1/ch1-s0-demo.png"' in resolved


def test_resolve_drops_unregistered_generated_reference() -> None:
    body = 'Intro.\n\n<BookFigure id="ghost-figure" />'

    resolved = _resolve_chapter_figures(body, {})

    assert "BookFigure" not in resolved
