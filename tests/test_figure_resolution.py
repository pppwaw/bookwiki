from __future__ import annotations

from html import unescape

from bookwiki.agents._helpers import body_figure_refs, chapter_document, prune_figure_refs
from bookwiki.convert.common import BOOK_FIGURE_TAG_RE, parse_book_figure_tag
from bookwiki.pipeline.nodes import _quiz_item_mdx, _resolve_chapter_figures, _source_figures
from bookwiki.schemas.quiz import QuizItem

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


def _quiz_item(figure_ref: str | None = None) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "q1",
        "question": "Which traversal order does the tree show?",
        "choices": ["DFS", "BFS"],
        "answer": "BFS",
        "explanation": "The frontier expands level by level.",
    }
    if figure_ref is not None:
        item["figure_ref"] = figure_ref
    return item


def test_quiz_item_mdx_emits_figure_placeholder_between_question_and_choices() -> None:
    mdx = _quiz_item_mdx(_quiz_item("paper-p001-b001"), 1)

    assert '<BookFigure id="paper-p001-b001" />' in mdx
    assert (
        mdx.index("</QuizQuestion>") < mdx.index("<BookFigure") < mdx.index("<QuizChoices>")
    )


def test_quiz_item_mdx_omits_figure_when_no_figure_ref() -> None:
    assert "<BookFigure" not in _quiz_item_mdx(_quiz_item(), 1)


def test_quiz_figure_ref_resolves_against_chapter_index() -> None:
    quiz_mdx = _quiz_item_mdx(_quiz_item("paper-p001-b001"), 1)
    body = f"Intro.\n\n<QuizBlock>\n{quiz_mdx}\n</QuizBlock>"

    resolved = _resolve_chapter_figures(body, {"paper-p001-b001": CANONICAL})

    assert CANONICAL in resolved
    assert '<BookFigure id="paper-p001-b001" />' not in resolved


def test_quiz_unknown_figure_ref_is_dropped() -> None:
    quiz_mdx = _quiz_item_mdx(_quiz_item("ghost-figure"), 1)
    body = f"Intro.\n\n<QuizBlock>\n{quiz_mdx}\n</QuizBlock>"

    resolved = _resolve_chapter_figures(body, {"paper-p001-b001": CANONICAL})

    assert "<BookFigure" not in resolved


def test_unreferenced_source_figure_is_not_appended_to_trailing_section() -> None:
    body = "Intro paragraph without a figure reference."

    resolved = _resolve_chapter_figures(body, {"paper-p001-b001": CANONICAL})

    assert resolved == body


def test_body_figure_refs_extracts_unique_ordered_ids() -> None:
    body = f"{CANONICAL}\n\n{ID_ONLY_TIGHT}\n\n{ID_ONLY_SPACED}"

    assert body_figure_refs(body) == ["paper-p001-b001", "paper-p002-b002"]


def test_prune_figure_refs_clears_ids_outside_the_allowed_set() -> None:
    items = [
        QuizItem(
            question="q",
            choices=["a", "b"],
            answer="a",
            explanation="e",
            figure_ref="paper-p001-b001",
        ),
        QuizItem(
            question="q",
            choices=["a", "b"],
            answer="a",
            explanation="e",
            figure_ref="ghost",
        ),
    ]

    prune_figure_refs(items, ["paper-p001-b001"])

    assert items[0].figure_ref == "paper-p001-b001"
    assert items[1].figure_ref == ""
