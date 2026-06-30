from __future__ import annotations

import html
import json
import re


def normalize_concept_links(mdx: str, alias_map: dict[str, str]) -> str:
    output = mdx
    for alias, canonical in alias_map.items():
        output = output.replace(f"[[{alias}]]", f"[[{canonical}]]")
    return output


def normalize_mdx_math(mdx: str) -> str:
    # Deterministically convert ``\[ ... \]`` / ``\( ... \)`` to ``$$``/``$`` — but ONLY
    # outside JSX prop string/array literals and code spans. That exclusion is the whole
    # point: the old document-wide rewrite (no JSX guard) injected raw newlines into JSON
    # string literals inside props like ``citations={[...]}`` and broke the MDX expression
    # parse at build time. Prose delimiters are auto-fixed here (the model occasionally
    # emits ``\(``/``\[`` despite the prompt contract); JSX props are left intact.
    normalized = _convert_latex_delimiters_outside_jsx(mdx)
    normalized = _repair_split_display_math_linebreaks(normalized)
    normalized = _normalize_katex_text_mode_chars(normalized)
    return _canonicalize_display_fences(normalized)


def normalize_mdx_for_validation(mdx: str) -> str:
    """Apply deterministic render-safety rewrites before MDX validation.

    This layer prevents bounded LLM repair loops from spending rounds on issues we can
    fix exactly: math fence/escape quirks, raw cite tags that map to ``SourceRef``, raw
    HTML style strings, and public asset Markdown image paths.
    """
    return convert_html_style_attrs(
        normalize_source_cites(normalize_mdx_math(normalize_public_asset_markdown_images(mdx)))
    )


_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)"
)


def normalize_public_asset_markdown_images(markdown: str) -> str:
    """Make Markdown image links to site public assets root-relative.

    Raw ``<BookFigure src=\"/bookwiki-assets/...\" />`` tags already use the served
    public URL. Plain Markdown images copied from source text can use
    ``bookwiki-assets/...`` without the leading slash; Fumadocs then compiles them as
    relative imports from the current MDX directory and Next cannot resolve them.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", markdown)
    return "".join(
        part if part.startswith("`") else _normalize_public_asset_image_segment(part)
        for part in parts
    )


def _normalize_public_asset_image_segment(segment: str) -> str:
    def replace(match: re.Match[str]) -> str:
        target = html.unescape(match.group("target")).strip()
        if target.startswith("bookwiki-assets/"):
            title = match.group("title") or ""
            asset_path = target.removeprefix("bookwiki-assets/")
            return f"![{match.group('alt')}](/bookwiki-assets/{asset_path}{title})"
        return match.group(0)

    return _MARKDOWN_IMAGE_RE.sub(replace, segment)


# Already-delimited math: `$$ ... $$` or `$ ... $`. If a quote already carries any of
# these, the LLM marked its math correctly (card/quiz agents do this) and we must not
# re-wrap it.
_DOLLAR_MATH_RE = re.compile(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$")

# Strong signals that an undelimited string is in fact LaTeX math. A backslash command
# from this whitelist, or an explicit sub/superscript group, is required before we will
# consider wrapping. A bare backslash is NOT enough (avoids "the command \frac means…").
_LATEX_MATH_SIGNAL_RE = re.compile(
    r"\\(?:d?frac|tfrac|sum|prod|int|oint|sqrt|ln|log|lim|sin|cos|tan|cot|sec|csc"
    r"|left|right|dots|cdots|ldots|vdots|ddots|rightarrow|leftarrow|to|mapsto|infty"
    r"|partial|nabla|cdot|times|div|pm|mp|leq|geq|neq|approx|equiv|sim|propto|forall"
    r"|exists|in|notin|subset|cup|cap|alpha|beta|gamma|delta|epsilon|varepsilon|zeta"
    r"|eta|theta|vartheta|iota|kappa|lambda|mu|nu|xi|pi|rho|sigma|tau|phi|varphi|chi"
    r"|psi|omega|Gamma|Delta|Theta|Lambda|Sigma|Phi|Psi|Omega|mathbb|mathcal|mathrm"
    r"|mathbf|operatorname|begin|end)\b"
    r"|[_^]\{"
)

# A "Label: math" split — a short natural-language prefix ending in a colon, followed by
# the math run (e.g. "The N-th partial sum: S_n = …").
_LABEL_SPLIT_RE = re.compile(r"^(?P<label>.{1,80}?[:：])\s*(?P<rest>\S[\s\S]*)$")

_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?。，；：！？]+$")


def _convert_latex_delimiters_to_dollar(text: str) -> str:
    """Rewrite ``\\[ ... \\]`` / ``\\( ... \\)`` delimiters to ``$$``/``$`` in a plain span.

    Operates on a single non-JSX, non-code span (callers exclude those). ``\\[ ... \\]``
    becomes a multi-line ``$$`` block, so the caller must guarantee the span is NOT a JSX
    prop / JSON string literal (where raw newlines would break the MDX expression parse).
    """
    text = re.sub(r"(?m)^[ \t]*\\\[[ \t]*$", "$$", text)
    text = re.sub(r"(?m)^[ \t]*\\\][ \t]*$", "$$", text)
    text = re.sub(
        r"\s*\\\[([\s\S]*?)\\\]\s*[.,;:]?",
        lambda match: f"\n\n$$\n{match.group(1).strip()}\n$$\n\n",
        text,
    )
    return re.sub(r"\\\(([\s\S]*?)\\\)", lambda match: f"${match.group(1).strip()}$", text)


def _convert_latex_delimiters_outside_jsx(mdx: str) -> str:
    """Apply ``_convert_latex_delimiters_to_dollar`` to prose only, leaving JSX prop
    string/array literals (``={"..."}`` / ``={[...]}``) and code spans untouched."""
    parts = _FENCE_EXCLUDE_RE.split(mdx)
    return "".join(
        part
        if part is None or part.startswith(("`", "={"))
        else _convert_latex_delimiters_to_dollar(part)
        for part in parts
        if part is not None
    )


def normalize_citation_quote_math(quote: str) -> str:
    """Ensure math inside a citation quote carries ``$`` delimiters so it renders as KaTeX.

    Conservative on purpose: only an entire pure-math run, or the pure-math suffix after a
    natural-language ``Label:`` prefix, is wrapped. Any quote that mixes prose words with
    math in an ambiguous way (e.g. ``\\frac{1}{n} is not unbiased``) is left untouched, and
    quotes that already contain ``$...$`` are returned unchanged (idempotent). This runs
    BEFORE ``_escape_mdx_text_outside_math`` so wrapped regions keep their raw ``{}`` for
    KaTeX instead of being HTML-escaped. ``\\[ \\]`` / ``\\( \\)`` delimiters are converted by
    ``normalize_mdx_math`` (a quote is plain text, never a JSX prop, so the conversion applies).
    """
    text = normalize_mdx_math(str(quote).strip())
    if not text or _DOLLAR_MATH_RE.search(text) or not _LATEX_MATH_SIGNAL_RE.search(text):
        return text

    if _is_pure_math_run(text):
        body, trailing = _split_trailing_punct(text)
        return f"${body}${trailing}" if body else text

    match = _LABEL_SPLIT_RE.match(text)
    if match and _is_pure_math_run(match.group("rest")):
        body, trailing = _split_trailing_punct(match.group("rest").strip())
        return f"{match.group('label')} ${body}${trailing}" if body else text

    return text


def _is_pure_math_run(segment: str) -> bool:
    """True when ``segment`` is math with no natural-language words.

    A natural-language word is a run of >=2 ASCII letters left over after LaTeX commands
    and math structure (braces, operators, sub/superscripts, digits, single-letter
    variables) are stripped. Its presence vetoes wrapping.
    """
    segment = segment.strip()
    if not segment or not _LATEX_MATH_SIGNAL_RE.search(segment):
        return False
    residue = re.sub(r"\\[a-zA-Z]+", " ", segment)  # drop \frac, \ln, \left, ...
    residue = re.sub(r"\\[^a-zA-Z]", " ", residue)  # drop \\, \{, \,, ...
    residue = re.sub(r"[{}\[\]()|^_=+\-*/<>~!?.,;:'\"`&%]", " ", residue)
    residue = re.sub(r"\d+", " ", residue)
    return not any(len(token) >= 2 and token.isalpha() for token in residue.split())


def _split_trailing_punct(segment: str) -> tuple[str, str]:
    match = _TRAILING_PUNCT_RE.search(segment)
    if match:
        return segment[: match.start()].rstrip(), segment[match.start() :]
    return segment, ""


# Spans that must never be touched by fence canonicalization: code fences, inline code,
# JSX string props (`={"..."}`, e.g. PreviewLink summary) and JSX array props
# (`={[...]}`, e.g. QuizItem citations) — their string literals may legally contain `$$`.
_FENCE_EXCLUDE_RE = re.compile(
    r"(```[\s\S]*?```|`[^`\n]*`|=\{\"(?:[^\"\\]|\\.)*\"\}|=\{\[[\s\S]*?\]\})"
)
_DISPLAY_FENCE_RE = re.compile(r"\$\$")

_MATH_SPAN_RE = re.compile(r"\$\$[\s\S]*?\$\$|\$[^$\n]+\$")
_TEXT_COMMAND_RE = re.compile(r"\\text\{([^{}]*)\}")
_KATEX_TEXT_MODE_DIGITS = str.maketrans(
    {
        "①": "1",
        "②": "2",
        "③": "3",
        "④": "4",
        "⑤": "5",
        "⑥": "6",
        "⑦": "7",
        "⑧": "8",
        "⑨": "9",
        "⑩": "10",
    }
)


_SPLIT_LINEBREAK_DISPLAY_RE = re.compile(
    r"(?P<br>\\{2,})[ \t]*\n[ \t]*\n?[ \t]*\$\$[ \t]*\n"
    r"[ \t]*(?P<spacing>\d+(?:\.\d+)?\s*(?:pt|em|ex|mu|mm|cm|in))\][ \t]*\n"
)


def _repair_split_display_math_linebreaks(mdx: str) -> str:
    """Repair a model-split ``\\[4pt]`` display linebreak.

    A common repair/generation artifact is:

    ``... , \\\n\n$$\n4pt]\n next aligned row ... $$``

    The middle ``$$`` prematurely closes the display block, leaving raw
    ``\\mathbf{...}`` in MDX prose where braces become JSX expressions. Joining it
    back to ``\\[4pt]``
    restores the intended aligned environment without guessing new math content.
    """
    return _SPLIT_LINEBREAK_DISPLAY_RE.sub(
        lambda match: match.group("br")[:2] + f"[{match.group('spacing')}]\n",
        mdx,
    )


def _canonicalize_display_fences(mdx: str) -> str:
    """Put every multi-line ``$$ ... $$`` display block on its own fence lines.

    remark-math only closes a flow-math block when the closing ``$$`` stands at the
    start of its own line; model output like ``$$f(x) =`` (content after the opening
    fence) or ``...},$$`` / ``$$证明 ...`` (content sharing the closing fence's line)
    silently fails to close, swallows the following prose into one giant math node,
    and desyncs every later ``$$`` pair — surfacing far away as an acorn parse error.
    Pairing ``$$`` tokens in document order and rewriting each multi-line pair to
    ``\\n\\n$$\\n<body>\\n$$\\n\\n`` restores the author's intended pairing. Single-line
    ``$$x$$`` spans are already valid and left untouched; segments with an odd number
    of fences are left alone rather than guessed at.
    """
    parts = _FENCE_EXCLUDE_RE.split(mdx)
    return "".join(
        part
        if part is None or part.startswith("`") or part.startswith("={")
        else _canonicalize_fence_segment(part)
        for part in parts
        if part is not None
    )


def _normalize_katex_text_mode_chars(mdx: str) -> str:
    """Avoid KaTeX text-mode font-metric warnings inside math spans.

    KaTeX has metrics for Greek letters in math fonts, but not for raw ``θ`` when it
    appears inside ``\text{...}``; circled digits in ``\tag{①}`` have the same issue.
    Normalize only already-delimited math spans so ordinary prose and JSX props keep
    their human-facing wording.
    """
    parts = _FENCE_EXCLUDE_RE.split(mdx)
    return "".join(
        part
        if part is None or part.startswith("`") or part.startswith("={")
        else _MATH_SPAN_RE.sub(lambda match: _normalize_katex_math(match.group(0)), part)
        for part in parts
        if part is not None
    )


def _normalize_katex_math(math_span: str) -> str:
    normalized = _undouble_latex_command_escapes(math_span.translate(_KATEX_TEXT_MODE_DIGITS))
    normalized = _normalize_latex_pipe_delimiters(normalized)
    return _TEXT_COMMAND_RE.sub(
        _normalize_katex_text_command,
        normalized,
    )


def _normalize_latex_pipe_delimiters(math_span: str) -> str:
    # A raw ``|`` is valid TeX, but inside Markdown tables it is still a table-cell
    # delimiter before the MDX math plugin sees it. Use the command form instead.
    return re.sub(r"\\(left|right|big|Big|bigl|bigr|Bigl|Bigr)\|", r"\\\1\\vert", math_span)


def _undouble_latex_command_escapes(math_span: str) -> str:
    # Model/JSON round-trips sometimes leave TeX commands as literal ``\\mathbf``
    # inside already-delimited math. KaTeX interprets that as a linebreak followed by
    # text, so commands like ``^\\circ`` fail. Only collapse doubled backslashes when
    # they introduce a command or a TeX spacing escape; keep real linebreaks ``\\``
    # before ``[4pt]``/newlines intact.
    return re.sub(r"\\\\(?=[A-Za-z ])", r"\\", math_span)


def _normalize_katex_text_command(match: re.Match[str]) -> str:
    text = match.group(1)
    if "θ" not in text:
        return match.group(0)

    pieces = text.split("θ")
    rendered: list[str] = []
    for index, piece in enumerate(pieces):
        if piece:
            rendered.append(f"\\text{{{piece}}}")
        if index < len(pieces) - 1:
            rendered.append(r"\theta")
    return "".join(rendered)


def _canonicalize_fence_segment(segment: str) -> str:
    positions = [match.start() for match in _DISPLAY_FENCE_RE.finditer(segment)]
    if not positions or len(positions) % 2 == 1:
        return segment
    out: list[str] = []
    last = 0
    for index in range(0, len(positions), 2):
        start, end = positions[index], positions[index + 1]
        inner = segment[start + 2 : end]
        if "\n" not in inner or _fence_pair_is_canonical(segment, start, end, inner):
            continue
        out.append(segment[last:start].rstrip(" \t"))
        out.append("\n\n$$\n" + inner.strip() + "\n$$\n\n")
        last = end + 2
    out.append(segment[last:])
    return "".join(out)


def _fence_pair_is_canonical(segment: str, start: int, end: int, inner: str) -> bool:
    """True when both fences already sit alone on their own lines (leave untouched)."""
    opener_at_line_start = start == 0 or segment[start - 1] == "\n"
    opener_alone = inner.startswith("\n")
    closer_alone = re.search(r"\n[ \t]*$", inner) is not None
    after = end + 2
    closer_ends_line = after >= len(segment) or segment[after] == "\n"
    return opener_at_line_start and opener_alone and closer_alone and closer_ends_line


def convert_html_style_attrs(mdx: str) -> str:
    """Convert raw-HTML string ``style="..."`` attrs to JSX ``style={{...}}`` objects.

    MDX compiles a string ``style`` prop fine (so the bundled compile-check passes), but
    React rejects it at render time ("The `style` prop expects a mapping ... not a
    string"), crashing the static site build. The model occasionally emits raw HTML like
    ``<div style="border:1px solid #ccc; padding: 1em">``; this deterministically
    rewrites it to a valid JSX style object so the rendered ``.mdx`` is render-safe.
    Code spans/fences are left untouched, and existing ``style={{...}}`` objects (no
    quote right after ``=``) are not matched.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", mdx)
    return "".join(part if part.startswith("`") else _convert_style_segment(part) for part in parts)


_STYLE_ATTR_RE = re.compile(r"""style=(?P<q>["'])(?P<css>.*?)(?P=q)""", re.DOTALL)


def _convert_style_segment(segment: str) -> str:
    return _STYLE_ATTR_RE.sub(_style_attr_to_jsx, segment)


def _style_attr_to_jsx(match: re.Match[str]) -> str:
    pairs: list[str] = []
    for declaration in match.group("css").split(";"):
        prop, sep, value = declaration.partition(":")
        if not sep:
            continue
        key = _css_to_camel(prop.strip())
        val = value.strip().replace("\\", "\\\\").replace("'", "\\'")
        if key and val:
            pairs.append(f"'{key}': '{val}'")
    # No usable declarations -> empty object, still a valid (render-safe) JSX style.
    return "style={{" + ", ".join(pairs) + "}}"


def normalize_source_cites(mdx: str) -> str:
    """Rewrite raw ``<cite ref=...>`` / ``<Citation ref_id=...>`` tags to the registered
    ``SourceRef`` MDX component.

    React treats ``ref`` as a special prop, so a model-emitted ``<cite ref="p001">``
    can compile as MDX but crash during Next prerender with "Refs cannot be used in
    Server Components". A model also sometimes hallucinates a capitalized ``<Citation>``
    component, which is not registered in ``getMDXComponents`` and crashes prerender with
    "Expected component `Citation` to be defined". ``SourceRef`` is the site-supported
    citation surface, so this deterministic rewrite preserves the ref id and quote while
    removing the offending tag before the page reaches React.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", mdx)
    return "".join(
        part if part.startswith("`") else _normalize_cite_segment(part) for part in parts
    )


_CITE_REF_RE = re.compile(
    r"<(?:cite|citation)\s+(?P<attr>ref|ref_id)=(?P<q>[\"'])(?P<id>.*?)(?P=q)"
    r"\s*(?:/\s*>|>(?P<quote>[\s\S]*?)</(?:cite|citation)\s*>)",
    re.IGNORECASE,
)


def _normalize_cite_segment(segment: str) -> str:
    return _CITE_REF_RE.sub(_cite_to_source_ref, segment)


def _cite_to_source_ref(match: re.Match[str]) -> str:
    ref_id = html.unescape(match.group("id")).strip()
    quote = _plain_text_from_inline_html(match.group("quote") or "")
    if not ref_id:
        return _escape_mdx_text_outside_math(quote)

    # The quote is shown in the SourceRef hover tooltip (and stays searchable in the
    # chapter "## Sources" list), so it must NOT also be duplicated as inline prose.
    return _source_ref_component(ref_id, quote)


def _source_ref_component(ref_id: str, quote: str) -> str:
    props = [f"id={{{json.dumps(ref_id, ensure_ascii=False)}}}"]
    if quote:
        props.append(f"quote={{{json.dumps(quote, ensure_ascii=False)}}}")
    return "<SourceRef " + " ".join(props) + " />"


def _plain_text_from_inline_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _escape_mdx_text_outside_math(markdown: str) -> str:
    parts = re.split(r"(\$\$[\s\S]*?\$\$|\$[^$\n]*\$|```[\s\S]*?```|`[^`\n]*`)", markdown)
    return "".join(
        part if part.startswith(("`", "$")) else _escape_mdx_text_segment(part) for part in parts
    )


def _escape_mdx_text_segment(segment: str) -> str:
    return (
        segment.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
    )


def _css_to_camel(prop: str) -> str:
    head, *rest = prop.split("-")
    return head + "".join(word[:1].upper() + word[1:] for word in rest if word)
