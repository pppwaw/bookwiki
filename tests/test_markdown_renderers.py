from __future__ import annotations

from bookwiki.integrator.markdown_renderers import (
    convert_html_style_attrs,
    normalize_mdx_math,
    normalize_source_cites,
)


def test_converts_html_style_string_to_jsx_object() -> None:
    # The exact chapter-7 case that crashed the Next/React prerender.
    out = convert_html_style_attrs(
        '<div style="border:1px solid #e0e0e0; padding: 1em; margin: 1em 0;">'
    )
    assert out == (
        "<div style={{'border': '1px solid #e0e0e0', 'padding': '1em', 'margin': '1em 0'}}>"
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


# --- display-fence canonicalization (the chapter-12-3 / 12-6 acorn breakage) --- #


def test_display_fence_with_content_on_opening_line_is_canonicalized() -> None:
    # `$$f(x) =` (content after the opening fence) never closes under remark-math.
    out = normalize_mdx_math("设\n$$f(x,y) =\n\\begin{cases}\n1\n\\end{cases}\n$$证明极限。\n")
    assert "\n$$\nf(x,y) =\n\\begin{cases}\n1\n\\end{cases}\n$$\n" in out
    # The prose that shared the closing fence's line is back outside the math block.
    assert "证明极限。" in out.split("$$")[-1]


def test_display_fence_with_content_before_closer_is_canonicalized() -> None:
    out = normalize_mdx_math("$$\\boxed{a,\\qquad\nb}},$$\n后文 $x$。\n")
    assert "\n$$\n\\boxed{a,\\qquad\nb}},\n$$\n" in out
    assert "后文 $x$。" in out.split("$$")[-1]


def test_valid_math_is_left_untouched() -> None:
    good = "前文\n\n$$\nE(X)=\\theta\n$$\n\n行内 $a+b$ 与单行 $$c=d$$ 结束。\n"
    assert normalize_mdx_math(good) == good


def test_odd_fence_count_segment_is_left_alone() -> None:
    odd = "孤立围栏 $$ 不要乱配对。\n"
    assert normalize_mdx_math(odd) == odd


def test_dollars_inside_jsx_props_and_code_are_untouched() -> None:
    jsx = (
        '<QuizItem id={"q1"} citations={[\n{\n"quote": "rule $$\\\\frac{a}{b}$$ end"\n}\n]}>\n'
        '<PreviewLink href={"/x"} summary={"含 $$ E(X) $$ 的摘要"}>词</PreviewLink>\n'
        "```\n$$\nraw\n$$\n```\n"
    )
    assert normalize_mdx_math(jsx) == jsx


def test_raw_cite_ref_is_rewritten_to_source_ref_component() -> None:
    out = normalize_source_cites(
        '<cite ref="12.4-p011">If $f_x$ and $f_y$ are continuous</cite>。'
    )

    assert out == (
        'If $f_x$ and $f_y$ are continuous '
        '<SourceRef id={"12.4-p011"} quote={"If $f_x$ and $f_y$ are continuous"} />。'
    )
    assert " ref=" not in out


def test_raw_cite_ref_id_variant_is_rewritten() -> None:
    out = normalize_source_cites("<cite ref_id='p001'>quoted text</cite>")

    assert out == 'quoted text <SourceRef id={"p001"} quote={"quoted text"} />'


def test_raw_cite_text_escapes_mdx_braces_outside_math() -> None:
    out = normalize_source_cites('<cite ref="p001">Solve {a} with $x_{1}$</cite>')

    assert out == (
        'Solve &#123;a&#125; with $x_{1}$ '
        '<SourceRef id={"p001"} quote={"Solve {a} with $x_{1}$"} />'
    )


def test_raw_cite_inside_code_is_untouched() -> None:
    code = '`<cite ref="p001">raw</cite>`\n```\n<cite ref="p002">raw</cite>\n```'

    assert normalize_source_cites(code) == code
