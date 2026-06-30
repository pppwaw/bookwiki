from __future__ import annotations

from bookwiki.integrator.markdown_renderers import (
    convert_html_style_attrs,
    normalize_mdx_for_validation,
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


def test_latex_delimiters_are_converted_to_dollar_in_prose() -> None:
    # Prose ``\( \)`` / ``\[ \]`` (occasional model contract slips) are deterministically
    # rewritten to ``$``/``$$`` so they render as KaTeX instead of literal brackets.
    inline = normalize_mdx_math("行内 \\(a+b\\) 公式。")
    assert inline == "行内 $a+b$ 公式。"

    display = normalize_mdx_math("独立 \\[E = mc^2\\] 公式。")
    assert "\\[" not in display
    assert "$$\nE = mc^2\n$$" in display


def test_latex_delimiters_inside_jsx_citation_prop_are_left_intact() -> None:
    # The exact 9.8-Taylor build break: a ``\[ ... \]`` quote inside ``citations={[...]}`` is
    # a JSON string literal and must survive byte-for-byte — the conversion must EXCLUDE JSX
    # prop spans, or it injects raw newlines + ``$$`` and acorn fails to parse the expression.
    import json

    quote = "几何级数 \\[ \\frac{1}{1-x} = 1 + x + \\cdots, \\ -1 < x < 1 \\]"
    payload = json.dumps([{"ref_id": "p020", "quote": quote}], ensure_ascii=False, indent=2)
    mdx = f"citations={{{payload}}}"
    assert normalize_mdx_math(mdx) == mdx


def test_valid_math_is_left_untouched() -> None:
    good = "前文\n\n$$\nE(X)=\\theta\n$$\n\n行内 $a+b$ 与单行 $$c=d$$ 结束。\n"
    assert normalize_mdx_math(good) == good


def test_split_display_linebreak_spacing_is_rejoined() -> None:
    mdx = "$$\n\\begin{aligned}\na &= b, \\\\\n\n$$\n4pt]\nc &= d\n\\end{aligned}\n$$\n"

    out = normalize_mdx_math(mdx)

    assert "$$\n4pt]" not in out
    assert "a &= b, \\\\[4pt]\n" in out
    assert "c &= d" in out


def test_doubled_latex_command_escapes_inside_math_are_collapsed() -> None:
    mdx = r"已知 $\\mathbf{V}_{Th}=10\\angle 0^\\circ\\ \\mathrm{V}$。"

    out = normalize_mdx_math(mdx)

    assert r"$\mathbf{V}_{Th}=10\angle 0^\circ\ \mathrm{V}$" in out


def test_latex_pipe_delimiter_inside_table_math_uses_command_form() -> None:
    mdx = r"| 数学联系 | $F(\omega) = F(s)\bigl|_{s=j\omega}$ | |" + "\n"

    out = normalize_mdx_math(mdx)

    assert r"\bigl\vert" in out
    assert r"\bigl|" not in out


def test_pre_validation_normalizer_applies_deterministic_render_fixes() -> None:
    mdx = (
        r"| 数学联系 | $F(\omega) = F(s)\bigl|_{s=j\omega}$ | |"
        "\n"
        '<cite ref="p001"/>\n'
        "![figure](bookwiki-assets/source/figure.jpg)\n"
        '<div style="margin-top: 4px">x</div>\n'
    )

    out = normalize_mdx_for_validation(mdx)

    assert r"\bigl\vert" in out
    assert '<SourceRef id={"p001"} />' in out
    assert "![figure](/bookwiki-assets/source/figure.jpg)" in out
    assert "style={{'marginTop': '4px'}}" in out


def test_katex_text_mode_unsupported_chars_are_normalized_inside_math_only() -> None:
    mdx = (
        "正文 θ 和步骤 ① 保持原样。\n"
        "$\\tag{①}$、$(②)$、$\\text{θ-简单}$。\n"
        '<Card description={"$\\tag{③}$ 与 θ 保持原样"} />\n'
        "`$\\tag{④}$`\n"
    )

    out = normalize_mdx_math(mdx)

    assert "正文 θ 和步骤 ① 保持原样。" in out
    assert "$\\tag{1}$" in out
    assert "$(2)$" in out
    assert "$\\theta\\text{-简单}$" in out
    assert '<Card description={"$\\tag{③}$ 与 θ 保持原样"} />' in out
    assert "`$\\tag{④}$`" in out


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
    out = normalize_source_cites('<cite ref="12.4-p011">If $f_x$ and $f_y$ are continuous</cite>。')

    # The quote lives only in the SourceRef hover tooltip; it is NOT duplicated inline.
    assert out == ('<SourceRef id={"12.4-p011"} quote={"If $f_x$ and $f_y$ are continuous"} />。')
    assert " ref=" not in out


def test_raw_cite_ref_id_variant_is_rewritten() -> None:
    out = normalize_source_cites("<cite ref_id='p001'>quoted text</cite>")

    assert out == '<SourceRef id={"p001"} quote={"quoted text"} />'


def test_self_closing_raw_cite_ref_is_rewritten() -> None:
    out = normalize_source_cites('<cite ref="p001"/>')

    assert out == '<SourceRef id={"p001"} />'


def test_capitalized_citation_tag_is_rewritten() -> None:
    """A model sometimes emits a hallucinated ``<Citation ref_id=.../>`` JSX component instead of
    the ``<cite>`` marker. ``Citation`` is not a registered MDX component, so it crashes the Next
    prerender with "Expected component `Citation` to be defined". Normalize it to ``SourceRef`` too.
    """
    out = normalize_source_cites('<Citation ref_id="14-.1-vector-field-p002"/>')
    assert out == '<SourceRef id={"14-.1-vector-field-p002"} />'

    paired = normalize_source_cites('<Citation ref="p001">quoted</Citation>')
    assert paired == '<SourceRef id={"p001"} quote={"quoted"} />'
    assert "Citation" not in out and "Citation" not in paired


def test_raw_cite_quote_prop_preserves_braces() -> None:
    out = normalize_source_cites('<cite ref="p001">Solve {a} with $x_{1}$</cite>')

    # Braces are preserved verbatim inside the JSON-encoded `quote` prop (a JS string,
    # not MDX text), so they must NOT be MDX-escaped to &#123;/&#125;.
    assert out == '<SourceRef id={"p001"} quote={"Solve {a} with $x_{1}$"} />'


def test_raw_cite_inside_code_is_untouched() -> None:
    code = '`<cite ref="p001">raw</cite>`\n```\n<cite ref="p002">raw</cite>\n```'

    assert normalize_source_cites(code) == code
