from __future__ import annotations

import json
import re
import shutil
from html import escape, unescape
from pathlib import Path
from typing import Any

import yaml

from bookwiki.checkers.quiz_extractor import (
    QuizExtractError,
    extract_inline_quizzes,
    extract_quiz_layout,
)
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.convert.common import (
    BOOK_FIGURE_TAG_RE,
    parse_book_figure_tag,
)
from bookwiki.integrator.exam_renderer import render_exam_mdx
from bookwiki.integrator.markdown_renderers import (
    convert_html_style_attrs,
    normalize_citation_quote_math,
    normalize_mdx_math,
    normalize_source_cites,
)
from bookwiki.integrator.markdown_renderers import (
    normalize_public_asset_markdown_images as _normalize_public_asset_markdown_images,
)
from bookwiki.integrator.stitching import audit_stitching
from bookwiki.pipeline._shared import (
    _EXAM_PAGE_FILENAME,
    _LOG,
    State,
    _agent_result,
    _citation_items,
    _clear_generated_files,
    _display_chapter_title,
    _load_skeleton,
    _mdx_link_exists,
    _rel,
    _safe_file_stem,
)
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.quiz import ExamResult
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text


def _mdx_prop(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _source_citation_md(citations: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in citations:
        ref_id = str(item.get("ref_id", "")).strip()
        quote = str(item.get("quote", "")).strip()
        if not ref_id:
            continue
        quote_md = _escape_mdx_text_outside_math(_source_quote_markdown(quote))
        lines.append(f"- `{ref_id}`: {quote_md}")
    return "\n".join(lines)


def _source_quote_markdown(quote: str) -> str:
    return normalize_citation_quote_math(quote)


def _normalize_chapter_body_heading(body_md: str, display_title: str) -> str:
    body = str(body_md).strip()
    if not display_title:
        return body
    if re.match(r"^#\s+.+$", body, flags=re.MULTILINE):
        return re.sub(r"^#\s+.+$", f"# {display_title}", body, count=1, flags=re.MULTILINE)
    return f"# {display_title}\n\n{body}" if body else f"# {display_title}"


def _escape_mdx_text_outside_math(markdown: str) -> str:
    parts = re.split(r"(\$\$[\s\S]*?\$\$|\$[^$\n]*\$|```[\s\S]*?```|`[^`\n]*`)", markdown)
    return "".join(
        part if part.startswith(("`", "$")) else _escape_mdx_text_segment(part) for part in parts
    )


def _escape_mdx_text_segment(segment: str) -> str:
    return (
        segment.replace("&", "&amp;")
        .replace("\\{", "&#123;")
        .replace("\\}", "&#125;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
    )


def _markdown_text(value: Any) -> str:
    return _escape_mdx_text_outside_math(normalize_mdx_math(str(value)))


def _jsx_prop(name: str, value: Any) -> str:
    return f"{name}={{{_mdx_prop(value)}}}"


def _mdx_child(tag: str, value: Any) -> str:
    return f"<{tag}>\n{_markdown_text(value).strip()}\n</{tag}>"


def _choice_id(index: int) -> str:
    return f"choice-{index}"


def _quiz_item_mdx(item: dict[str, Any], index: int) -> str:
    choices = [_markdown_text(choice) for choice in item.get("choices", [])]
    answer = _markdown_text(item.get("answer", "")).strip()
    matched_answer_id = next(
        (
            _choice_id(choice_index)
            for choice_index, choice in enumerate(choices, start=1)
            if choice.strip() == answer
        ),
        None,
    )
    answer_id = matched_answer_id or f"invalid-answer-{index:03d}"
    props = " ".join(
        [
            _jsx_prop("id", str(item.get("id") or f"quiz-{index:03d}")),
            _jsx_prop("answer", answer_id),
            _jsx_prop("citations", _citation_items(item.get("citations", []))),
        ]
    )
    choice_mdx = "\n".join(
        f"<QuizChoice {_jsx_prop('id', _choice_id(choice_index))}>\n{choice}\n</QuizChoice>"
        for choice_index, choice in enumerate(choices, start=1)
    )
    figure_ref = str(item.get("figure_ref") or "").strip()
    # A <BookFigure id=.. /> placeholder here is resolved against the chapter figure
    # index by `_resolve_chapter_figures` (which runs after quiz insertion), so the quiz
    # shows the real image. An unknown/missing id is dropped there, leaving no figure.
    figure_mdx = f'<BookFigure id="{escape(figure_ref, quote=True)}" />' if figure_ref else ""
    children = [
        f"<QuizItem {props}>",
        _mdx_child("QuizQuestion", item.get("question", "")),
        *([figure_mdx] if figure_mdx else []),
        "<QuizChoices>",
        choice_mdx,
        "</QuizChoices>",
        "<QuizCheck />",
        _mdx_child("QuizExplanation", item.get("explanation", "")),
        "</QuizItem>",
    ]
    return "\n".join(children)


def _worked_problem_mdx(item: dict[str, Any], index: int) -> str:
    slot_id = str(item.get("slot_id") or "")
    chapter_id = slot_id.split(":", 1)[0] if ":" in slot_id else ""
    props = " ".join(
        [
            _jsx_prop("id", str(item.get("id") or f"worked-{index:03d}")),
            _jsx_prop("chapterId", chapter_id),
            _jsx_prop("question", item.get("question", "")),
            _jsx_prop("referenceAnswer", item.get("reference_answer", "")),
            _jsx_prop("rubric", item.get("rubric", [])),
            _jsx_prop("explanation", item.get("explanation", "")),
            _jsx_prop("citations", _citation_items(item.get("citations", []))),
        ]
    )
    return f"<WorkedProblem {props}>\n</WorkedProblem>"


def _card_item_mdx(item: dict[str, Any], index: int) -> str:
    props = " ".join(
        [
            _jsx_prop("id", str(item.get("id") or f"card-{index:03d}")),
            _jsx_prop("citations", _citation_items(item.get("citations", []))),
        ]
    )
    return "\n".join(
        [
            f"<AnkiCard {props}>",
            _mdx_child("AnkiFront", item.get("front", "")),
            _mdx_child("AnkiBack", item.get("back", "")),
            "</AnkiCard>",
        ]
    )


def _card_items_mdx(items: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for index, item in enumerate(items, start=1):
        rendered.append(_card_item_mdx(item, index))
    return "\n\n".join(rendered)


def _frontmatter(data: dict[str, Any]) -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{body}\n---\n\n"


def _homepage_description(title: str, language: str | None) -> str:
    if str(language or "").lower().startswith("en"):
        return f"{title} learning home, table of contents, and key concepts."
    return f"{title} 的互动学习指南：章节目录与核心概念。"


def _home_cards_mdx(entries: list[dict[str, str]]) -> list[str]:
    lines = ["<Cards>"]
    for entry in entries:
        props = [
            _jsx_prop("title", entry["title"]),
            _jsx_prop("href", entry["href"]),
        ]
        if entry.get("description"):
            props.append(_jsx_prop("description", entry["description"]))
        lines.append(f"  <Card {' '.join(props)} />")
    lines.append("</Cards>")
    return lines


def _book_homepage_mdx(
    title: str,
    description: str,
    chapter_entries: list[dict[str, str]],
    concept_entries: list[dict[str, str]],
) -> str:
    lines = [
        _frontmatter({"title": title, "description": description}).rstrip(),
        "",
        "## 目录",
        "",
    ]
    if chapter_entries:
        lines.extend(_home_cards_mdx(chapter_entries))
    else:
        lines.append("暂无章节内容。")

    lines.extend(["", "## 概念", ""])
    if concept_entries:
        lines.extend(
            _home_cards_mdx(sorted(concept_entries, key=lambda entry: entry["title"].casefold()))
        )
    else:
        lines.append("暂无概念页。")

    return "\n".join(lines) + "\n"


def _homepage_summary(value: Any) -> str:
    paragraphs = [part.strip() for part in str(value or "").split("\n\n") if part.strip()]
    if not paragraphs:
        return ""
    return re.sub(r"\s+", " ", paragraphs[0]).strip()


def _load_alias_map(state: State, cfg: BookConfig) -> dict[str, str]:
    raw = state.get("alias_map", {})
    if isinstance(raw, dict):
        if raw:
            return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, str):
        data = read_json(cfg.book_dir / raw, default={})
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
    reconciled = state.get("reconciled_concepts")
    if isinstance(reconciled, str):
        data = read_json(cfg.book_dir / reconciled, default={})
        alias_map = data.get("alias_map", {}) if isinstance(data, dict) else {}
        if isinstance(alias_map, dict):
            return {str(key): str(value) for key, value in alias_map.items()}
    return {}


_QUIZ_BLOCK_RE = re.compile(r"<QuizBlock>[\s\S]*?</QuizBlock>")
# A naive ``[^>]*/>`` stops at the first ``>`` inside an attribute value (e.g.
# ``topic="determine i(t) for t>0"`` / ``给定一个仅在 t>0``), so the slot never matches and is
# neither filled nor dropped — it leaks the raw <QuizItemSlot> to the build (undefined component,
# prerender crash). Skip over quoted values so a ``>`` inside ``"..."``/``'...'`` can't end early.
_QUIZ_ITEM_SLOT_RE = re.compile(r"""<QuizItemSlot\b(?:"[^"]*"|'[^']*'|[^>])*?/>""")
_EMPTY_QUIZ_BLOCK_RE = re.compile(r"<QuizBlock>\s*</QuizBlock>\n*")


def _stash_quiz_blocks(markdown: str) -> tuple[str, list[str]]:
    """Replace authored ``<QuizBlock>`` spans with opaque sentinels.

    Concept auto-linking and ``[[wikilink]]`` rewriting must never reach inside an authored
    quiz block (no ``<PreviewLink>`` injected into question/choice/explanation text).
    """
    stash: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\x00QUIZBLOCK{len(stash) - 1}\x00"

    return _QUIZ_BLOCK_RE.sub(_sub, markdown), stash


def _unstash_quiz_blocks(markdown: str, stash: list[str]) -> str:
    for index, original in enumerate(stash):
        markdown = markdown.replace(f"\x00QUIZBLOCK{index}\x00", original)
    return markdown


# Fenced code blocks (```lang ... ```, e.g. ```mermaid) must survive concept-link
# normalization untouched: their inner text is not prose, so injecting <PreviewLink>
# or rewriting ``[[node]]`` shapes would corrupt the diagram / code. The per-line
# auto-linker only protects same-line spans, so multi-line fences are stashed whole.
_CODE_FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")


def _stash_code_fences(markdown: str) -> tuple[str, list[str]]:
    stash: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\x00CODEFENCE{len(stash) - 1}\x00"

    return _CODE_FENCE_RE.sub(_sub, markdown), stash


def _unstash_code_fences(markdown: str, stash: list[str]) -> str:
    for index, original in enumerate(stash):
        markdown = markdown.replace(f"\x00CODEFENCE{index}\x00", original)
    return markdown


def _collapse_blank_runs_outside_code(markdown: str) -> str:
    """Collapse 3+ consecutive newlines (left where a slot/block was removed) to a single blank
    line, leaving fenced code blocks — which may carry intentional blank lines — untouched."""
    stashed, fences = _stash_code_fences(markdown)
    stashed = re.sub(r"\n{3,}", "\n\n", stashed)
    return _unstash_code_fences(stashed, fences)


def _index_items_by_slot(quiz: dict[str, Any]) -> dict[str, tuple[dict[str, Any], int, str]]:
    """Map canonical ``slot_id`` -> (item, 1-based index, kind). Fail loud on a slotless item."""
    items_by_slot: dict[str, tuple[dict[str, Any], int, str]] = {}
    for index, item in enumerate(quiz.get("items", []), start=1):
        if not isinstance(item, dict):
            continue
        slot_id = str(item.get("slot_id") or "")
        if not slot_id:
            raise ValueError(
                "quiz item has no slot_id (stale after_block artifact; regenerate): "
                f"{str(item.get('question', ''))[:60]}"
            )
        items_by_slot[slot_id] = (item, index, "mcq")

    for index, item in enumerate(quiz.get("worked_items", []), start=1):
        if not isinstance(item, dict):
            continue
        slot_id = str(item.get("slot_id") or "")
        if not slot_id:
            raise ValueError(
                "worked quiz item has no slot_id (regenerate): "
                f"{str(item.get('question', ''))[:60]}"
            )
        items_by_slot[slot_id] = (item, index, "worked")
    return items_by_slot


def _render_filled_slot(
    slot_id: str, items_by_slot: dict[str, tuple[dict[str, Any], int, str]]
) -> str | None:
    """Render the ``<QuizItem>`` filling ``slot_id``, or ``None`` if no item matches the slot."""
    entry = items_by_slot.get(slot_id)
    if entry is None:
        return None
    item, index, kind = entry
    return _worked_problem_mdx(item, index) if kind == "worked" else _quiz_item_mdx(item, index)


def _resolve_item_slots_via_regex(
    body_md: str, items_by_slot: dict[str, tuple[dict[str, Any], int, str]]
) -> str:
    """Fallback slot resolver for when ``body_md`` is not yet MDX-parseable (AST unavailable).

    Uses the quote-aware ``_QUIZ_ITEM_SLOT_RE`` (which tolerates a ``>`` inside an attribute value)
    so slots are still filled/dropped rather than leaked, then drops any block left empty.
    """

    def _replace(match: re.Match[str]) -> str:
        id_match = re.search(r'id="([^"]*)"', match.group(0))
        return _render_filled_slot(id_match.group(1) if id_match else "", items_by_slot) or ""

    resolved = _QUIZ_ITEM_SLOT_RE.sub(_replace, body_md)
    return _EMPTY_QUIZ_BLOCK_RE.sub("", resolved)


def _resolve_item_slots(body_md: str, quiz: dict[str, Any]) -> str:
    """Replace each inline ``<QuizItemSlot id=X/>`` with its filled application ``<QuizItem>``.

    Slots are located by the remark AST (precise source offsets), NOT regex: a regex over the raw
    text mis-parses a literal ``>`` inside an attribute value (``topic="... for t>0"`` / ``t>0``)
    and leaves the slot in place, leaking its raw tag to the build (undefined ``QuizItemSlot``
    component -> prerender crash). The AST also surfaces slots sitting OUTSIDE a ``<QuizBlock>``
    (stray placeholders) that a block-only / line-based scan misses.

    Knowledge quizzes are already authored inline in ``body_md``; only application slots need
    filling. Items are matched to slots by canonical ``slot_id``. A slot with no matching item is
    removed, and a ``<QuizBlock>`` left empty is removed too. An item carrying no ``slot_id`` is a
    stale ``after_block``-era artifact and is a hard error (regenerate the chapter).
    """
    items_by_slot = _index_items_by_slot(quiz)
    try:
        layout = extract_quiz_layout(body_md)
    except QuizExtractError as exc:
        # Body not yet MDX-parseable (a chapter heal is still pending) or the Node toolchain is
        # missing: resolve by regex so slots are never silently leaked to the build.
        _LOG.warning("resolve slots: AST unavailable, using regex fallback: %s", exc)
        return _resolve_item_slots_via_regex(body_md, items_by_slot)

    # (start, end, replacement) spans into body_md, applied right-to-left so earlier offsets stay
    # valid. Knowledge <QuizItem>s and other prose are left untouched (never in this list).
    spans: list[tuple[int, int, str]] = []
    for block in layout["blocks"]:
        children = block.get("children", [])
        slot_fills = [
            (c, _render_filled_slot(str(c.get("id") or ""), items_by_slot))
            for c in children
            if c.get("kind") == "slot"
        ]
        keeps_content = any(c.get("kind") in {"item", "unknown"} for c in children) or any(
            text is not None for _, text in slot_fills
        )
        b_start, b_end = block.get("start"), block.get("end")
        if not keeps_content and isinstance(b_start, int) and isinstance(b_end, int):
            # Every child is an unfilled slot -> the block would be empty; drop it whole.
            spans.append((b_start, b_end, ""))
            continue
        for child, text in slot_fills:
            start, end = child.get("start"), child.get("end")
            if isinstance(start, int) and isinstance(end, int):
                spans.append((start, end, text or ""))

    for slot in layout["stray_slots"]:
        start, end = slot.get("start"), slot.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        filled = _render_filled_slot(str(slot.get("id") or ""), items_by_slot)
        # A stray slot sits outside any <QuizBlock>; if it matches an item, wrap the filled item in
        # its own block so the Deck context exists, otherwise drop the bare placeholder.
        spans.append((start, end, f"<QuizBlock>\n{filled}\n</QuizBlock>" if filled else ""))

    resolved = body_md
    for start, end, text in sorted(spans, key=lambda s: s[0], reverse=True):
        resolved = resolved[:start] + text + resolved[end:]
    return _collapse_blank_runs_outside_code(resolved)


def _drop_invalid_inline_quiz_items(text: str) -> str:
    """Remove rendered inline quiz items whose answer cannot resolve to a choice.

    Generation-time sanitization covers normal authored quiz blocks, but legacy or
    fallback-repaired bodies can reach integration with malformed <QuizItem>s. Dropping
    only the invalid item preserves the surrounding prose and any valid sibling items.
    """
    try:
        blocks = extract_inline_quizzes(text)
    except QuizExtractError:
        return text

    spans: list[tuple[int, int]] = []
    for block in blocks:
        for child in block.get("children", []):
            if child.get("kind") != "item":
                continue
            choice_ids = {str(choice.get("id")) for choice in child.get("choices", [])}
            answer = str(child.get("answer") or "")
            if answer not in choice_ids:
                start, end = child.get("start"), child.get("end")
                if isinstance(start, int) and isinstance(end, int):
                    spans.append((start, end))

    if not spans:
        return text

    cleaned = text
    for start, end in sorted(spans, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = _EMPTY_QUIZ_BLOCK_RE.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"


def _strip_stray_quiz_slots(text: str) -> str:
    """Drop any ``<QuizItemSlot/>`` that survived into rendered MDX (and the empty block it leaves).

    A slot is a generation-time placeholder; integrate's ``_resolve_item_slots`` either fills it
    into a ``<QuizItem>`` or drops it. One reaching the single-source-of-truth ``content`` is an
    unfilled placeholder whose component is NOT registered in the site MDX provider, so it crashes
    the production prerender ("Expected component QuizItemSlot to be defined"). Stripping honors the
    drop-never-fabricate contract and is the final deterministic guard that keeps the build green.
    """
    stripped = _QUIZ_ITEM_SLOT_RE.sub("", text)
    if stripped == text:
        return text
    return _EMPTY_QUIZ_BLOCK_RE.sub("", stripped)


def _normalize_rendered_mdx(content_dir: Path) -> int:
    """Normalize math delimiters + strip stray quiz slots across every rendered ``.mdx``.

    integrate normalizes chapter/concept bodies as it renders them, but the homepage ``index.mdx``
    and any other write path are not individually covered. One final sweep here guarantees the
    single-source-of-truth ``content`` is fully normalized before validation/build — this replaces
    the second normalize pass that used to live in ``materialize_site``. It also strips any stray
    ``<QuizItemSlot/>`` as a build-safety backstop (see :func:`_strip_stray_quiz_slots`). Returns
    files changed.
    """
    changed = 0
    for path in content_dir.rglob("*.mdx"):
        text = path.read_text(encoding="utf-8")
        normalized = _strip_stray_quiz_slots(normalize_mdx_math(text))
        if normalized != text:
            path.write_text(normalized, encoding="utf-8")
            changed += 1
    return changed


def _normalize_concept_links(
    markdown: str,
    alias_map: dict[str, str],
    concept_previews: dict[str, dict[str, str]],
    chapter_previews: dict[str, dict[str, str]] | None = None,
    *,
    auto_link: bool = True,
    suppress: set[str] | None = None,
) -> str:
    markdown, fence_stash = _stash_code_fences(markdown)
    markdown, quiz_stash = _stash_quiz_blocks(markdown)
    # ``suppress`` seeds the "already linked" set so those canonicals are never auto-linked. Concept
    # pages pass their own name here so a page never self-links its own term throughout its body.
    linked_canonicals: set[str] = set(suppress or ())
    chapters = chapter_previews or {}

    def replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        # ``[[...]]`` is the explicit cross-reference syntax. Per the authoring contract
        # (see COMMON_SYSTEM_PROMPT) bare prose terms auto-link to concepts, so an author only
        # reaches for ``[[name]]`` when they mean a *chapter*. Resolve chapters first; a name that
        # is both a concept and a chapter therefore links to the chapter (the author's intent).
        chapter = chapters.get(label) or chapters.get(_concept_key(label))
        if chapter:
            return _preview_link_mdx(
                chapter["href"], chapter["title"], chapter["summary"], chapter["title"]
            )
        # No chapter matched: fall back to a concept link by (alias-resolved) canonical name. This
        # is the only way concept pages (auto_link=False) link to other concepts.
        canonical = alias_map.get(label) or alias_map.get(_concept_key(label)) or label
        preview = concept_previews.get(canonical)
        if preview:
            linked_canonicals.add(canonical)
            return _preview_link_mdx(
                preview["href"], preview["title"], preview["summary"], canonical
            )
        return f"[[{canonical}]]"

    normalized = re.sub(r"\[\[([^\]]+)\]\]", replace, markdown)
    # ``auto_link`` turns bare prose terms into concept links. Both chapter bodies and concept
    # pages use it so the contract is uniform (bare prose -> concept, ``[[ ]]`` -> chapter). A
    # concept page suppresses its own name (see ``suppress``) so it links *other* concepts only.
    if auto_link:
        normalized = _auto_link_concept_terms(
            normalized, _concept_link_terms(alias_map, concept_previews), linked_canonicals
        )
    return _unstash_code_fences(_unstash_quiz_blocks(normalized, quiz_stash), fence_stash)


def _concept_link_terms(
    alias_map: dict[str, str], concept_previews: dict[str, dict[str, str]]
) -> list[tuple[str, str, dict[str, str]]]:
    terms: dict[str, tuple[str, str, dict[str, str]]] = {}
    for canonical, preview in concept_previews.items():
        clean = str(canonical).strip()
        if clean:
            terms[_concept_key(clean)] = (clean, clean, preview)
    for alias, canonical in alias_map.items():
        clean_alias = str(alias).strip()
        clean_canonical = str(canonical).strip()
        preview = concept_previews.get(clean_canonical)
        if clean_alias and preview:
            terms[_concept_key(clean_alias)] = (clean_alias, clean_canonical, preview)
    return sorted(terms.values(), key=lambda item: len(item[0]), reverse=True)


_CONCEPT_LINK_PROTECTED_RE = re.compile(
    r"(<PreviewLink\b[\s\S]*?</PreviewLink>|```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\[[^\]\n]+\]\([^)]+\)|<[^>\n]+>)"
)


def _auto_link_concept_terms(
    markdown: str,
    terms: list[tuple[str, str, dict[str, str]]],
    linked_canonicals: set[str],
) -> str:
    if not terms:
        return markdown
    # Split protected spans on the WHOLE document first, so MULTI-LINE ``$$ ... $$`` display
    # math and fenced code blocks are excluded as single units. Splitting per line first let
    # the interior lines of a multi-line ``$$`` block look like prose and get a <PreviewLink>
    # injected inside the math (e.g. ``$\operatorname{<PreviewLink ...$``), which breaks the
    # MDX/KaTeX parse. The ``[\s\S]*?`` in the regex only works when applied across lines.
    out: list[str] = []
    for part in _CONCEPT_LINK_PROTECTED_RE.split(markdown):
        if not part:
            continue
        if _CONCEPT_LINK_PROTECTED_RE.fullmatch(part):
            out.append(part)
        else:
            # Prose between protected spans: still skip heading lines so titles stay unlinked.
            out.append(
                "".join(
                    line
                    if line.lstrip().startswith("#")
                    else _auto_link_concept_terms_in_text(line, terms, linked_canonicals)
                    for line in part.splitlines(keepends=True)
                )
            )
    return "".join(out)


def _auto_link_concept_terms_in_text(
    text: str,
    terms: list[tuple[str, str, dict[str, str]]],
    linked_canonicals: set[str],
) -> str:
    candidates: list[tuple[int, int, str, dict[str, str]]] = []
    for term, canonical, preview in terms:
        if canonical in linked_canonicals:
            continue
        pattern = _concept_term_pattern(term)
        flags = re.IGNORECASE if re.search(r"[A-Za-z]", term) else 0
        for match in re.finditer(pattern, text, flags=flags):
            candidates.append((match.start(), match.end(), canonical, preview))
    if not candidates:
        return text

    selected: list[tuple[int, int, str, dict[str, str]]] = []
    local_linked: set[str] = set()
    occupied_until = -1
    for start, end, canonical, preview in sorted(
        candidates, key=lambda item: (item[0], -(item[1] - item[0]))
    ):
        if canonical in linked_canonicals or canonical in local_linked:
            continue
        if start < occupied_until:
            continue
        selected.append((start, end, canonical, preview))
        local_linked.add(canonical)
        occupied_until = end
    if not selected:
        return text

    chunks: list[str] = []
    cursor = 0
    for start, end, canonical, preview in selected:
        chunks.append(text[cursor:start])
        chunks.append(
            _preview_link_mdx(
                preview["href"], preview["title"], preview["summary"], text[start:end]
            )
        )
        cursor = end
        linked_canonicals.add(canonical)
    chunks.append(text[cursor:])
    return "".join(chunks)


def _preview_link_mdx(href: str, title: str, summary: str, label: str) -> str:
    props = " ".join(
        [
            _jsx_prop("href", href),
            _jsx_prop("title", title),
            _jsx_prop("summary", summary),
        ]
    )
    return f"<PreviewLink {props}>{_markdown_text(label).strip()}</PreviewLink>"


def _preview_summary(markdown: str, *, max_chars: int = 180) -> str:
    text = re.sub(r"^---\n.*?\n---", " ", str(markdown), flags=re.DOTALL)
    text = next((part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()), "")
    text = normalize_mdx_math(text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"<[^>\n]+>", " ", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^[#>*\-\s]+", " ", text, flags=re.MULTILINE)
    text = re.sub(r"[`*_~]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _concept_term_pattern(term: str) -> str:
    escaped = re.escape(term)
    if re.search(r"[A-Za-z0-9]", term):
        return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return escaped


_LOCAL_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[(?P<label>[^\]\n]+)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)


def _drop_missing_local_markdown_links(markdown: str, base_dir: Path) -> str:
    """Turn markdown links to missing local files into plain text labels.

    This keeps generated explanatory prose while preventing check/index from being wedged by
    model-invented chapter/section paths such as /Chapter-12-.../section-003.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", markdown)
    return "".join(
        part if part.startswith("`") else _drop_missing_local_markdown_links_segment(part, base_dir)
        for part in parts
    )


def _drop_missing_local_markdown_links_segment(segment: str, base_dir: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        target = unescape(match.group("target")).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        if _mdx_link_exists(base_dir, target):
            return match.group(0)
        return match.group("label")

    return _LOCAL_MARKDOWN_LINK_RE.sub(replace, segment)


def _chapter_figure_index(state: State, cfg: BookConfig, ch_id: str) -> dict[str, str]:
    """Map ``figure_id -> canonical <BookFigure/> tag`` for one chapter's source.

    The chapter source markdown produced by ``split`` carries the fully-rendered
    figure tags verbatim. Returns an empty mapping when the chapter has no source
    (e.g. older states), but a declared-yet-missing source file fails loudly.
    """
    rel_source = state.get("chapter_sources", {}).get(ch_id)
    if not rel_source:
        return {}
    source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
    index: dict[str, str] = {}
    for tag in BOOK_FIGURE_TAG_RE.findall(source_md):
        figure_id = unescape(parse_book_figure_tag(tag).get("id", ""))
        if figure_id and figure_id not in index:
            index[figure_id] = tag
    # Merge figures generated during ``generate`` (run_plot output) so their
    # inline <BookFigure/> references survive ``_resolve_chapter_figures``.
    generated = state.get("generated_figures", {}).get(ch_id, {})
    if isinstance(generated, dict):
        for figure_id, tag in generated.items():
            index.setdefault(str(figure_id), str(tag))
    # A <BookFigure/> without a `src` has no image asset; keeping it would render an
    # empty caption-only box (a phantom "missing image", e.g. next to a quiz). Drop
    # those so only figures that actually show the referenced image survive.
    return {
        figure_id: tag
        for figure_id, tag in index.items()
        if unescape(parse_book_figure_tag(tag).get("src", "")).strip()
    }


def _resolve_chapter_figures(body: str, index: dict[str, str]) -> str:
    """Resolve inline ``<BookFigure/>`` references against the chapter figure index.

    Known references are rewritten to their canonical (src/caption-bearing) tag;
    unknown references are dropped so hallucinated ids never reach the page. Figures
    present in the source but never referenced are omitted: the learning page should
    only show figures placed by the generated chapter body or referenced by quiz
    items.
    """

    def _replace(match: re.Match[str]) -> str:
        figure_id = unescape(parse_book_figure_tag(match.group(0)).get("id", ""))
        canonical = index.get(figure_id)
        if canonical is None:
            return ""
        return canonical

    return BOOK_FIGURE_TAG_RE.sub(_replace, body)


# Homepage concept-graph pruning caps: keep the most-connected backbone only
# (top-N nodes by degree, then top-M edges). Large books would otherwise emit a
# hairball.
_GRAPH_MAX_NODES = 120
_GRAPH_MAX_EDGES = 400


def _graph_summary(markdown: str, *, limit: int = 140) -> str:
    """Short hover-card summary that KEEPS LaTeX (``$ \\ _ ^``).

    Unlike ``_preview_summary`` (which strips ``_`` and other markers), this
    preserves math so the site can render ``$...$`` spans with KaTeX in the
    concept-graph tooltip. Truncation never cuts inside a ``$...$`` span.
    """
    text = re.sub(r"\s+", " ", str(markdown or "").strip())
    text = text.replace("**", "").replace("`", "")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if cut.count("$") % 2 == 1:  # don't truncate inside a $...$ math span
        cut = cut[: cut.rfind("$")]
    return cut.rstrip() + "…"


def _emit_concept_graph(state: State, cfg: BookConfig) -> Path | None:
    """Emit ``work/concept-graph.json`` for the homepage force-directed graph.

    Nodes are concepts (slug, display name, owning-chapter group for colour,
    degree, LaTeX-preserving summary). Edges come from each concept's
    ``related`` field, resolved to canonical slugs via the skeleton glossary +
    ``alias_map``; unresolved targets are DROPPED (never fabricated). A double
    Top-N prune keeps the backbone: top-N nodes by degree, then top-M edges,
    with a connectivity guarantee, then any node left edgeless is dropped (the
    homepage graph is about relationships). ``materialize_site`` copies the file
    to ``site/public/concept-graph.json`` where the homepage fetches it.
    """
    concept_pages = state.get("concept_pages", {}) or {}
    if not concept_pages:
        return None

    skeleton = _load_skeleton(state, cfg) or {}
    glossary = skeleton.get("glossary", []) or []
    alias_map = skeleton.get("alias_map", {}) or {}

    chapter_of: dict[str, str] = {}
    for entry in glossary:
        canonical = str(entry.get("canonical", ""))
        if canonical:
            chapter_of[_concept_key(canonical)] = str(entry.get("first_chapter_id") or "misc")

    nodes: dict[str, dict[str, Any]] = {}
    name_to_slug: dict[str, str] = {}
    raw_related: dict[str, list[str]] = {}
    for name, rel_path in concept_pages.items():
        slug = Path(rel_path).stem
        if not slug:
            continue
        concept = read_json(cfg.book_dir / rel_path, default={})
        concept_name = str(concept.get("name") or name)
        nodes[slug] = {
            "id": slug,
            "name": concept_name,
            "slug": slug,
            "group": chapter_of.get(_concept_key(concept_name), "misc"),
            "summary": _graph_summary(concept.get("summary_md") or concept.get("body_md", "")),
            "degree": 0,
        }
        name_to_slug[_concept_key(concept_name)] = slug
        raw_related[slug] = [str(r) for r in (concept.get("related") or [])]

    # Skeleton aliases resolve to the owning concept's slug too.
    for entry in glossary:
        slug = name_to_slug.get(_concept_key(str(entry.get("canonical", ""))))
        if not slug:
            continue
        for alias in [entry.get("canonical", ""), *entry.get("aliases", [])]:
            name_to_slug.setdefault(_concept_key(str(alias)), slug)

    def resolve(target: str) -> str | None:
        key = _concept_key(target)
        if key in name_to_slug:
            return name_to_slug[key]
        canon = alias_map.get(key) or alias_map.get(target)
        if canon and _concept_key(str(canon)) in name_to_slug:
            return name_to_slug[_concept_key(str(canon))]
        return None

    edges: dict[tuple[str, str], int] = {}
    for slug, related in raw_related.items():
        for target in related:
            tslug = resolve(target)
            if not tslug or tslug == slug or tslug not in nodes:
                continue
            key = tuple(sorted((slug, tslug)))
            edges[key] = edges.get(key, 0) + 1

    for a, b in edges:
        nodes[a]["degree"] += 1
        nodes[b]["degree"] += 1

    # ---- Double Top-N: prune nodes by degree, then edges ----
    ranked = sorted(nodes.values(), key=lambda n: (-n["degree"], n["name"]))
    kept = {n["slug"] for n in ranked[:_GRAPH_MAX_NODES]}
    within = sorted(
        ((a, b, w) for (a, b), w in edges.items() if a in kept and b in kept),
        key=lambda e: -(nodes[e[0]]["degree"] + nodes[e[1]]["degree"]),
    )
    kept_edges = [{"source": a, "target": b, "weight": w} for a, b, w in within[:_GRAPH_MAX_EDGES]]

    # Connectivity guarantee: every kept node keeps at least its strongest edge.
    connected = {e["source"] for e in kept_edges} | {e["target"] for e in kept_edges}
    for a, b, w in within:
        if a not in connected or b not in connected:
            kept_edges.append({"source": a, "target": b, "weight": w})
            connected.add(a)
            connected.add(b)

    # The graph is about relationships → drop nodes that ended up edgeless.
    kept = {s for s in kept if s in connected}
    kept_edges = [e for e in kept_edges if e["source"] in kept and e["target"] in kept]
    kept_nodes = [nodes[s] for s in kept]

    out_path = cfg.work_dir / "concept-graph.json"
    write_json(out_path, {"nodes": kept_nodes, "edges": kept_edges})
    _LOG.info(
        "concept graph emitted book_id=%s nodes=%d edges=%d",
        cfg.book_id,
        len(kept_nodes),
        len(kept_edges),
    )
    return out_path


def _write_exam_page(cfg: BookConfig, chapter_dir: Path, exam_rel: str, display_title: str) -> None:
    """Render a chapter's exam artifact to ``<chapter>/exam.mdx`` and write the folder meta.

    ``mode`` is read off the owner task id (``:explain`` → past-paper walkthrough, else a
    generated chapter exam), so the same renderer drives both surfaces.
    """

    exam = ExamResult.model_validate(_agent_result(read_json(cfg.book_dir / exam_rel)))
    mode = "walkthrough" if exam.owner_task_id.endswith(":explain") else "exam"
    page_title = f"{display_title} · {'讲解' if mode == 'walkthrough' else '测验'}"
    # An exam page is its own page type (NOT ``chapter``): the site only appends the
    # Feynman panel / lists in the home chapter index for ``chapter``/``concept`` pages,
    # and an exam is neither — it is a standalone paper that should carry neither.
    write_text(
        chapter_dir / _EXAM_PAGE_FILENAME,
        _frontmatter({"title": page_title, "type": "exam"}) + render_exam_mdx(exam, mode=mode),
    )
    write_json(chapter_dir / "meta.json", {"title": display_title, "pages": ["index", "exam"]})


def integrate_node(state: State, cfg: BookConfig) -> State:
    # Lay the Next.js framework into ``site`` before rendering content into it. ``content`` is in
    # SKIP/PRESERVE, so this never clobbers the docs we are about to (re)render. Idempotent.
    from scripts.site import scaffold_site_template

    scaffold_site_template(cfg)
    content_dir = ensure_dir(cfg.content_dir)
    chapters_dir = content_dir / "chapters"
    # Rebuilt from scratch each run: recursive removal clears both flat and nested
    # (two-level group) chapter files/dirs so a rerun never leaves stale pages behind.
    if chapters_dir.exists():
        shutil.rmtree(chapters_dir)
    chapters_dir = ensure_dir(chapters_dir)
    concepts_dir = ensure_dir(content_dir / "concepts")
    _clear_generated_files(concepts_dir, "*.mdx")
    chapter_home_entries: list[dict[str, str]] = []
    concept_home_entries: list[dict[str, str]] = []
    concept_backlinks: dict[str, list[dict[str, str]]] = {}
    alias_map = _load_alias_map(state, cfg)
    concept_previews: dict[str, dict[str, str]] = {}
    chapter_groups = state.get("chapter_groups", {}) or {}
    _LOG.info(
        "integrate: chapters=%d concepts=%d alias_map=%d groups=%d",
        len(state.get("agent_results", {})),
        len(state.get("concept_pages", {})),
        len(alias_map),
        len(chapter_groups),
    )
    leaf_to_group: dict[str, str] = {}
    group_titles: dict[str, str] = {}
    for group_id, group_info in chapter_groups.items():
        info = group_info if isinstance(group_info, dict) else {}
        group_titles[str(group_id)] = str(info.get("title") or group_id)
        for leaf_id in info.get("leaf_ids", []) or []:
            leaf_to_group[str(leaf_id)] = str(group_id)
    group_page_lists: dict[str, list[str]] = {str(gid): [] for gid in chapter_groups}
    top_level_pages: list[str] = []
    seen_groups: set[str] = set()
    for name, rel_path in state.get("concept_pages", {}).items():
        concept = read_json(cfg.book_dir / rel_path)
        concept_name = str(concept.get("name") or name)
        stem = Path(rel_path).stem
        preview = {
            "href": f"/docs/concepts/{stem}",
            "title": concept_name,
            "summary": _preview_summary(
                str(concept.get("summary_md") or concept.get("body_md", ""))
            ),
        }
        concept_previews[str(name)] = preview
        concept_previews[concept_name] = preview

    agent_results = state.get("agent_results", {})
    # ``order_index`` must follow the authoritative reading order (approved-structure / YAML order
    # persisted at split), never the iteration order of the ``agent_results`` dict — which can be
    # polluted by a partial regenerate, a stale checkpoint, or a reconcile merge.
    authoritative_order = list(state.get("chapter_order") or list(agent_results.keys()))
    order_index_by_id = {ch_id: idx for idx, ch_id in enumerate(authoritative_order)}
    next_order_index = len(order_index_by_id)
    # Pre-pass: build chapter-to-chapter link previews keyed by the chapter's display title (and
    # its normalized key), so a ``[[chapter title]]`` wikilink in any chapter body resolves to the
    # owning chapter page. Runs before the render loop, which normalizes each body inline.
    chapter_previews: dict[str, dict[str, str]] = {}
    for ch_id, paths in agent_results.items():
        ch = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        summ = _agent_result(read_json(cfg.book_dir / paths["summary"]))
        display_title = _display_chapter_title(ch_id, str(ch["title"]))
        group_id = leaf_to_group.get(ch_id)
        doc_slug = f"{group_id}/{ch_id}" if group_id else ch_id
        preview = {
            "href": f"/docs/chapters/{doc_slug}",
            "title": display_title,
            "summary": _preview_summary(str(summ.get("summary_md", ""))),
        }
        chapter_previews[display_title] = preview
        chapter_previews[_concept_key(display_title)] = preview
    for ch_id, paths in agent_results.items():
        chapter_order_index = order_index_by_id.get(ch_id)
        if chapter_order_index is None:
            chapter_order_index = next_order_index
            next_order_index += 1
        chapter = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        summary = _agent_result(read_json(cfg.book_dir / paths["summary"]))
        quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
        card = _agent_result(read_json(cfg.book_dir / paths["card"]))
        citations = chapter.get("citations", [])
        citation_md = _source_citation_md(citations)
        card_items = [item for item in card.get("items", []) if isinstance(item, dict)]
        card_ids = [
            str(item.get("id") or f"card-{index:03d}")
            for index, item in enumerate(card_items, start=1)
        ]
        display_title = _display_chapter_title(ch_id, str(chapter["title"]))
        body_md = _normalize_chapter_body_heading(str(chapter["body_md"]), display_title)
        group_id = leaf_to_group.get(ch_id)
        doc_slug = f"{group_id}/{ch_id}" if group_id else ch_id
        chapter_href = f"/docs/chapters/{doc_slug}"
        card_mdx = (
            f"<AnkiDeck {_jsx_prop('cardIds', card_ids)}>\n"
            f"{_card_items_mdx(card_items)}\n"
            "</AnkiDeck>"
        )
        concept_names = [str(name) for name in chapter.get("concepts", [])]
        for name in concept_names:
            concept_backlinks.setdefault(name, []).append(
                {
                    "title": display_title,
                    "href": chapter_href,
                    "summary": str(summary["summary_md"]),
                }
            )
        rendered_body = _resolve_item_slots(
            convert_html_style_attrs(
                normalize_source_cites(
                    normalize_mdx_math(
                        _normalize_concept_links(
                            body_md, alias_map, concept_previews, chapter_previews
                        )
                    )
                )
            ),
            quiz,
        )
        resolved_body = _resolve_chapter_figures(
            rendered_body, _chapter_figure_index(state, cfg, ch_id)
        )
        # A chapter that has an exam becomes a folder (`<slug>/index.mdx` + `<slug>/exam.mdx`)
        # so the exam is its own page within the chapter; otherwise it stays a flat `<slug>.mdx`.
        exam_rel = paths.get("exam")
        if exam_rel:
            chapter_path = ensure_dir(chapters_dir / doc_slug) / "index.mdx"
        else:
            chapter_path = chapters_dir / f"{doc_slug}.mdx"
        ensure_dir(chapter_path.parent)
        resolved_body = _normalize_public_asset_markdown_images(resolved_body)
        resolved_body = _drop_missing_local_markdown_links(
            _drop_invalid_inline_quiz_items(resolved_body), chapter_path.parent
        )
        chapter_frontmatter = {
            "chapter_id": ch_id,
            "title": display_title,
            "type": "chapter",
            "order_index": chapter_order_index,
            "summary": summary["summary_md"],
            "concepts": concept_names,
        }
        # key_points carry inline LaTeX and are rendered by the site's <Markdown> component,
        # so normalize their math like body prose — most importantly undoubling JSON
        # round-tripped escapes (``\\nabla`` -> ``\nabla``; KaTeX reads ``\\`` as a linebreak,
        # which is the "莫名换行 + 命令不渲染" bug in the 要点清单).
        chapter_key_points = [
            normalize_mdx_math(str(item)).strip() for item in summary.get("key_points", []) if item
        ]
        chapter_frontmatter["key_points"] = chapter_key_points
        write_text(
            chapter_path,
            (
                _frontmatter(chapter_frontmatter)
                + resolved_body
                + "\n\n"
                + f"## Sources\n\n{citation_md}\n\n"
                + f"## Anki Cards\n\n{card_mdx}\n"
            ),
        )
        if exam_rel:
            _write_exam_page(cfg, chapter_path.parent, exam_rel, display_title)
        if group_id:
            if group_id not in seen_groups:
                seen_groups.add(group_id)
                top_level_pages.append(group_id)
            group_page_lists.setdefault(group_id, []).append(ch_id)
        else:
            top_level_pages.append(ch_id)
        chapter_home_entries.append(
            {
                "title": display_title,
                "href": chapter_href,
                "description": _homepage_summary(summary.get("summary_md", "")),
            }
        )

    for name, rel_path in state.get("concept_pages", {}).items():
        concept = read_json(cfg.book_dir / rel_path)
        safe_name = Path(rel_path).stem or _safe_file_stem(name, fallback_prefix="concept")
        backlinks = concept_backlinks.get(str(name)) or concept_backlinks.get(
            str(concept["name"]), []
        )
        backlink_md = "\n".join(
            "- "
            + _preview_link_mdx(item["href"], item["title"], item.get("summary", ""), item["title"])
            for item in backlinks
        )
        referenced_by = f"\n\n## Referenced By\n\n{backlink_md}\n" if backlink_md else ""
        concept_path = concepts_dir / f"{safe_name}.mdx"
        concept_body = convert_html_style_attrs(
            normalize_source_cites(
                normalize_mdx_math(
                    _normalize_concept_links(
                        str(concept["body_md"]),
                        alias_map,
                        concept_previews,
                        chapter_previews,
                        auto_link=True,
                        # Suppress self-linking: a concept page auto-links *other* concepts but
                        # never its own name (id + canonical) throughout its body.
                        suppress={str(name), str(concept["name"])},
                    )
                )
            )
        )
        concept_body = _normalize_public_asset_markdown_images(concept_body)
        concept_body = _drop_missing_local_markdown_links(concept_body, concept_path.parent)
        concept_frontmatter = {
            "title": concept["name"],
            "type": "concept",
        }
        concept_summary = str(concept.get("summary_md", "")).strip()
        if concept_summary:
            concept_frontmatter["summary"] = concept_summary
        write_text(
            concept_path,
            _frontmatter(concept_frontmatter)
            + f"# {concept['name']}\n\n"
            + concept_body
            + referenced_by,
        )
        concept_name = str(concept["name"])
        concept_preview = concept_previews.get(concept_name) or concept_previews.get(str(name), {})
        concept_home_entries.append(
            {
                "title": concept_name,
                "href": f"/docs/concepts/{safe_name}",
                "description": concept_preview.get("summary", ""),
            }
        )

    index_path = write_text(
        content_dir / "index.mdx",
        _book_homepage_mdx(
            cfg.title,
            _homepage_description(cfg.title, cfg.language),
            chapter_home_entries,
            concept_home_entries,
        ),
    )
    concept_stem_list = sorted(
        {Path(rel_path).stem for rel_path in state.get("concept_pages", {}).values()}
    )
    write_json(
        content_dir / "meta.json",
        {
            "title": cfg.title,
            "pages": ["index", "chapters", "concepts"],
        },
    )
    write_json(
        chapters_dir / "meta.json",
        {
            "title": "Chapters",
            "root": True,
            "icon": "BookOpen",
            "pages": top_level_pages,
        },
    )
    for group_id, leaf_ids in group_page_lists.items():
        if not leaf_ids:
            continue
        write_json(
            chapters_dir / group_id / "meta.json",
            {
                "title": group_titles.get(group_id, group_id),
                "pages": leaf_ids,
            },
        )
    write_json(
        concepts_dir / "meta.json",
        {
            "title": "Concepts",
            "root": True,
            "icon": "Library",
            "pages": concept_stem_list,
        },
    )
    _emit_concept_graph(state, cfg)
    # scaffold ran before the graph existed; publish it now that integrate has emitted it.
    graph_src = cfg.work_dir / "concept-graph.json"
    if graph_src.exists():
        graph_dst = cfg.site_dir / "public" / "concept-graph.json"
        graph_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(graph_src, graph_dst)
    normalized = _normalize_rendered_mdx(content_dir)
    _LOG.info("integrate: normalized math in %d mdx file(s)", normalized)
    stitching = audit_stitching(content_dir, alias_map)
    if not stitching.ok:
        _LOG.warning(
            "stitching audit found issues book_id=%s term_drift=%d unresolved_xrefs=%d",
            cfg.book_id,
            len(stitching.term_drift),
            len(stitching.unresolved_xrefs),
        )
    _LOG.info(
        "integrate: done chapters_written=%d concepts_written=%d home_chapters=%d home_concepts=%d "
        "stitching_ok=%s",
        len(chapter_home_entries),
        len(concept_home_entries),
        len(chapter_home_entries),
        len(concept_home_entries),
        stitching.ok,
    )
    return {"content_ready": True, "content_index": _rel(index_path, cfg.book_dir)}


__all__ = [
    "_mdx_prop",
    "_source_citation_md",
    "_source_quote_markdown",
    "_normalize_chapter_body_heading",
    "_escape_mdx_text_outside_math",
    "_escape_mdx_text_segment",
    "_markdown_text",
    "_jsx_prop",
    "_mdx_child",
    "_choice_id",
    "_quiz_item_mdx",
    "_worked_problem_mdx",
    "_card_item_mdx",
    "_card_items_mdx",
    "_frontmatter",
    "_homepage_description",
    "_home_cards_mdx",
    "_book_homepage_mdx",
    "_homepage_summary",
    "_load_alias_map",
    "_QUIZ_BLOCK_RE",
    "_QUIZ_ITEM_SLOT_RE",
    "_EMPTY_QUIZ_BLOCK_RE",
    "_stash_quiz_blocks",
    "_unstash_quiz_blocks",
    "_CODE_FENCE_RE",
    "_stash_code_fences",
    "_unstash_code_fences",
    "_collapse_blank_runs_outside_code",
    "_index_items_by_slot",
    "_render_filled_slot",
    "_resolve_item_slots_via_regex",
    "_resolve_item_slots",
    "_drop_invalid_inline_quiz_items",
    "_strip_stray_quiz_slots",
    "_normalize_rendered_mdx",
    "_normalize_concept_links",
    "_concept_link_terms",
    "_CONCEPT_LINK_PROTECTED_RE",
    "_auto_link_concept_terms",
    "_auto_link_concept_terms_in_text",
    "_preview_link_mdx",
    "_preview_summary",
    "_concept_term_pattern",
    "_LOCAL_MARKDOWN_LINK_RE",
    "_drop_missing_local_markdown_links",
    "_drop_missing_local_markdown_links_segment",
    "_chapter_figure_index",
    "_resolve_chapter_figures",
    "_GRAPH_MAX_NODES",
    "_GRAPH_MAX_EDGES",
    "_graph_summary",
    "_emit_concept_graph",
    "_write_exam_page",
    "integrate_node",
]
