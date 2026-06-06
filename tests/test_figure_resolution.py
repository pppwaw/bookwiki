from __future__ import annotations

from html import unescape

from bookwiki.agents._helpers import chapter_document
from bookwiki.convert.common import BOOK_FIGURE_TAG_RE, parse_book_figure_tag
from bookwiki.pipeline.nodes import _source_figures

CANONICAL = (
    '<BookFigure id="paper-p001-b001" sourceRef="paper-p001" '
    'src="/bookwiki-assets/paper/a.png" caption="A &amp; B &lt;fig&gt;" />'
)
ID_ONLY_SPACED = '<BookFigure id="paper-p001-b001" />'
ID_ONLY_TIGHT = '<BookFigure id="paper-p002-b002"/>'


def test_book_figure_tag_re_matches_canonical_and_id_only_tags() -> None:
    body = (
        "Intro paragraph.\n\n"
        f"{CANONICAL}\n\n"
        "Middle paragraph.\n\n"
        f"{ID_ONLY_SPACED}\n\n"
        f"{ID_ONLY_TIGHT}\n"
    )

    matches = BOOK_FIGURE_TAG_RE.findall(body)

    assert matches == [CANONICAL, ID_ONLY_SPACED, ID_ONLY_TIGHT]


def test_parse_book_figure_tag_returns_raw_escaped_attributes() -> None:
    attrs = parse_book_figure_tag(CANONICAL)

    assert attrs == {
        "id": "paper-p001-b001",
        "sourceRef": "paper-p001",
        "src": "/bookwiki-assets/paper/a.png",
        "caption": "A &amp; B &lt;fig&gt;",
    }
    # Values are returned still-escaped; the caller unescapes.
    assert unescape(attrs["caption"]) == "A & B <fig>"


def test_parse_book_figure_tag_handles_id_only_tag() -> None:
    assert parse_book_figure_tag(ID_ONLY_SPACED) == {"id": "paper-p001-b001"}
    assert parse_book_figure_tag(ID_ONLY_TIGHT) == {"id": "paper-p002-b002"}


def test_chapter_document_replaces_figures_with_readable_placeholders() -> None:
    inp = {
        "source_md": (
            "# Search\n\n"
            "<!-- source_ref: paper-p001 -->\n\n"
            "Intro about search.\n\n"
            '<BookFigure id="paper-p001-b001" sourceRef="paper-p001" '
            'src="/bookwiki-assets/paper/a.png" caption="Search tree diagram" />\n\n'
            '<BookFigure id="paper-p001-b002" sourceRef="paper-p001" '
            'src="/bookwiki-assets/paper/b.png" />\n\n'
            "Closing text.\n"
        )
    }

    document = chapter_document(inp)

    assert "[Figure paper-p001-b001: Search tree diagram]" in document
    assert "[Figure paper-p001-b002]" in document
    # No raw or HTML-escaped figure tag should survive into the prompt document.
    assert "BookFigure" not in document
    assert "&lt;BookFigure" not in document


def test_source_figures_dedupes_and_unescapes_captions() -> None:
    source_md = (
        "# Search\n\n"
        '<BookFigure id="paper-p001-b001" sourceRef="paper-p001" '
        'src="/bookwiki-assets/paper/a.png" caption="A &amp; B &lt;fig&gt;" />\n\n'
        "Body text.\n\n"
        '<BookFigure id="paper-p002-b002"/>\n\n'
        # A duplicate id must be dropped (first occurrence wins).
        '<BookFigure id="paper-p001-b001" src="/bookwiki-assets/paper/a.png" />\n'
    )

    figures = _source_figures(source_md)

    assert figures == [
        {"id": "paper-p001-b001", "caption": "A & B <fig>"},
        {"id": "paper-p002-b002"},
    ]

