from __future__ import annotations

from bookwiki.integrator.markdown_renderers import convert_html_style_attrs


def test_converts_html_style_string_to_jsx_object() -> None:
    # The exact chapter-7 case that crashed the Next/React prerender.
    out = convert_html_style_attrs(
        '<div style="border:1px solid #e0e0e0; padding: 1em; margin: 1em 0;">'
    )
    assert out == (
        "<div style={{'border': '1px solid #e0e0e0', "
        "'padding': '1em', 'margin': '1em 0'}}>"
    )


def test_css_hyphenated_props_become_camelcase() -> None:
    out = convert_html_style_attrs('<span style="margin-top: 4px; font-weight: bold">x</span>')
    assert out == "<span style={{'marginTop': '4px', 'fontWeight': 'bold'}}>x</span>"


def test_existing_jsx_object_style_is_untouched() -> None:
    # No quote right after ``=`` -> not a string attr -> left alone.
    assert convert_html_style_attrs("<div style={{margin: 0}}>") == "<div style={{margin: 0}}>"


def test_style_inside_code_span_is_untouched() -> None:
    assert convert_html_style_attrs('`<div style="x: y">`') == '`<div style="x: y">`'
    fenced = '```\n<div style="x: y">\n```'
    assert convert_html_style_attrs(fenced) == fenced


def test_single_quoted_style_is_converted() -> None:
    assert convert_html_style_attrs("<p style='color: red'>") == "<p style={{'color': 'red'}}>"


def test_empty_or_malformed_style_becomes_empty_object() -> None:
    # An unusable style must still become a valid (render-safe) JSX object, never a string.
    assert convert_html_style_attrs('<div style="">') == "<div style={{}}>"
    assert convert_html_style_attrs('<div style="garbage-no-colon">') == "<div style={{}}>"
