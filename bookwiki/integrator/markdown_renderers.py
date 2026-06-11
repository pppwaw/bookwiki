from __future__ import annotations

import re


def normalize_concept_links(mdx: str, alias_map: dict[str, str]) -> str:
    output = mdx
    for alias, canonical in alias_map.items():
        output = output.replace(f"[[{alias}]]", f"[[{canonical}]]")
    return output


def normalize_mdx_math(mdx: str) -> str:
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", mdx)
    return "".join(
        part if part.startswith("`") else _normalize_math_segment(part) for part in parts
    )


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
    return "".join(
        part if part.startswith("`") else _convert_style_segment(part) for part in parts
    )


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


def _css_to_camel(prop: str) -> str:
    head, *rest = prop.split("-")
    return head + "".join(word[:1].upper() + word[1:] for word in rest if word)



def _normalize_math_segment(segment: str) -> str:
    segment = re.sub(
        r"\s*\\\[([\s\S]*?)\\\]\s*[.,;:]?",
        lambda match: f"\n\n$$\n{match.group(1).strip()}\n$$\n\n",
        segment,
    )
    return re.sub(
        r"\\\(([\s\S]*?)\\\)",
        lambda match: f"${match.group(1).strip()}$",
        segment,
    )
