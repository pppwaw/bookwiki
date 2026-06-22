"""Validate, ground, and canonicalize inline quizzes authored in section/chapter MDX.

SectionAgent authors knowledge quizzes inline (a full ``<QuizBlock>`` with ``<QuizItem>``s
written directly into the prose) and marks application quizzes with item-level
``<QuizItemSlot ... />`` placeholders inside an authored ``<QuizBlock>``. This module is the
safety net: it reverse-parses those tags via :mod:`bookwiki.checkers.quiz_extractor`,
validates each child, grounds citations / source refs against the chapter's allowed refs,
enforces per-block / per-section caps, dedupes slot specs, assigns canonical slot ids, and
re-renders each block in place (preserving the authored child order). Invalid items/slots
are dropped (never fabricated); a block left empty is removed.

The canonical render mirrors ``bookwiki.pipeline.nodes._quiz_item_mdx`` so the output
matches what the MDX validator and the fumadocs ``<QuizBlock>`` component accept. It is
duplicated here (not imported) because ``nodes`` imports ``generate.sections`` and
importing it back would create a cycle.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape
from typing import Any

from bookwiki.checkers.quiz_extractor import QuizExtractError, extract_inline_quizzes
from bookwiki.integrator.markdown_renderers import normalize_mdx_math
from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

MAX_ITEMS_PER_BLOCK = 6
MAX_ITEMS_PER_SECTION = 8
MAX_TOPIC_CHARS = 200

_STRAY_SLOT_RE = re.compile(r"<QuizItemSlot\b[^>]*/>")


@dataclass
class SlotSpec:
    """An application-quiz placeholder's generation spec, extracted from a ``<QuizItemSlot/>``."""

    slot_id: str
    topic: str
    concept: str
    source_refs: list[str]


@dataclass
class SanitizeResult:
    body_md: str
    slot_specs: list[SlotSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- canonical render (mirror of nodes._quiz_item_mdx) ---------------------------------


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


def _escape_mdx_text_outside_math(markdown: str) -> str:
    parts = re.split(r"(\$\$[\s\S]*?\$\$|\$[^$\n]*\$|```[\s\S]*?```|`[^`\n]*`)", markdown)
    return "".join(
        part if part.startswith(("`", "$")) else _escape_mdx_text_segment(part) for part in parts
    )


def _markdown_text(value: Any) -> str:
    return _escape_mdx_text_outside_math(normalize_mdx_math(str(value))).strip()


def _citations_prop(citations: list[dict[str, str]]) -> str:
    payload = [
        {"ref_id": str(item.get("ref_id", "")), "quote": str(item.get("quote", ""))}
        for item in citations
    ]
    return f"citations={{{json.dumps(payload, ensure_ascii=False, indent=2)}}}"


def _render_item(item: dict[str, Any], item_id: str) -> str:
    choices: list[dict[str, str]] = item["choices"]
    answer_id = item["answer_id"]
    choice_mdx = "\n".join(
        f'<QuizChoice id="choice-{index}">\n{_markdown_text(choice["text"])}\n</QuizChoice>'
        for index, choice in enumerate(choices, start=1)
    )
    props = (
        f'id="{escape(item_id, quote=True)}" '
        f'answer="{escape(answer_id, quote=True)}" '
        f"{_citations_prop(item['citations'])}"
    )
    figure_ref = str(item.get("figure_ref") or "").strip()
    figure_mdx = f'<BookFigure id="{escape(figure_ref, quote=True)}" />' if figure_ref else ""
    parts = [
        f"<QuizItem {props}>",
        f"<QuizQuestion>\n{_markdown_text(item['question'])}\n</QuizQuestion>",
        *([figure_mdx] if figure_mdx else []),
        "<QuizChoices>",
        choice_mdx,
        "</QuizChoices>",
        "<QuizCheck />",
        f"<QuizExplanation>\n{_markdown_text(item['explanation'])}\n</QuizExplanation>",
        "</QuizItem>",
    ]
    return "\n".join(parts)


def _render_slot(spec: SlotSpec) -> str:
    refs = json.dumps(spec.source_refs, ensure_ascii=False)
    attrs = [
        f'id="{escape(spec.slot_id, quote=True)}"',
        f'topic="{escape(spec.topic, quote=True)}"',
    ]
    if spec.concept:
        attrs.append(f'concept="{escape(spec.concept, quote=True)}"')
    attrs.append(f"sourceRefs={{{refs}}}")
    return f"<QuizItemSlot {' '.join(attrs)} />"


def _render_block(children: list[tuple[str, Any]]) -> str:
    parts: list[str] = []
    for kind, obj in children:
        parts.append(obj if kind == "item" else _render_slot(obj))
    return "<QuizBlock>\n" + "\n\n".join(parts) + "\n</QuizBlock>"


# --- validation ------------------------------------------------------------------------


def _valid_item(child: dict[str, Any], allowed_refs: set[str]) -> dict[str, Any] | None:
    question = str(child.get("question") or "").strip()
    explanation = str(child.get("explanation") or "").strip()
    raw_choices = child.get("choices") or []
    choices = [
        {"id": str(c.get("id") or ""), "text": str(c.get("text") or "").strip()}
        for c in raw_choices
        if str(c.get("text") or "").strip()
    ]
    if not question or not explanation or len(choices) < 2:
        return None
    answer = str(child.get("answer") or "")
    answer_index = next((i for i, c in enumerate(choices) if c["id"] == answer), -1)
    if answer_index < 0:
        return None
    citations_field = child.get("citations") or {}
    raw_citations = citations_field.get("value") if citations_field.get("ok") else []
    grounded = [
        c
        for c in (raw_citations or [])
        if isinstance(c, dict) and str(c.get("ref_id", "")) in allowed_refs
    ]
    return {
        "question": question,
        "explanation": explanation,
        "choices": choices,
        "answer_id": f"choice-{answer_index + 1}",
        "citations": grounded,
        "figure_ref": str(child.get("figure_ref") or "").strip(),
    }


def _valid_slot(child: dict[str, Any], allowed_refs: set[str]) -> tuple[str, list[str]] | None:
    topic = str(child.get("topic") or "").strip()
    if not topic or len(topic) > MAX_TOPIC_CHARS:
        return None
    refs_field = child.get("sourceRefs") or {}
    if not refs_field.get("ok"):
        return None
    raw_refs = refs_field.get("value") or []
    refs = [str(r) for r in raw_refs if str(r) in allowed_refs]
    if not refs:
        return None
    return topic, refs


# --- regex fallback (used only when the section body isn't MDX-parseable yet) ----------

_SLOT_TOPIC_RE = re.compile(r'\btopic="([^"]*)"')
_SLOT_CONCEPT_RE = re.compile(r'\bconcept="([^"]*)"')
_SLOT_SOURCEREFS_RE = re.compile(r"sourceRefs=\{\s*\[([^\]]*)\]\s*\}")
_QUOTED_STRING_RE = re.compile(r'"([^"]*)"')


def _rescue_slots_via_regex(
    body_md: str,
    *,
    allowed_refs: set[str],
    chapter_id: str,
    section_index: int,
    max_slots: int,
) -> tuple[str, list[SlotSpec], list[str]]:
    """Best-effort rescue of application ``<QuizItemSlot/>`` specs when ``body_md`` is not yet
    MDX-parseable.

    Mirrors the AST path's slot handling — non-empty ``topic`` (``<= MAX_TOPIC_CHARS``),
    ``sourceRefs`` grounded in ``allowed_refs``, dedup, section cap, canonical id stamping — but
    by regex, leaving knowledge ``<QuizItem>`` blocks untouched (they need the AST and are
    validated/canonicalized by the chapter-level MDX heal). Returns the rewritten body, the
    rescued specs, and warnings.
    """
    specs: list[SlotSpec] = []
    warnings: list[str] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    ordinal = 0
    changed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal ordinal, changed
        changed = True
        tag = match.group(0)
        topic_m = _SLOT_TOPIC_RE.search(tag)
        topic = topic_m.group(1).strip() if topic_m else ""
        concept_m = _SLOT_CONCEPT_RE.search(tag)
        concept = concept_m.group(1).strip() if concept_m else ""
        refs_m = _SLOT_SOURCEREFS_RE.search(tag)
        raw_refs = _QUOTED_STRING_RE.findall(refs_m.group(1)) if refs_m else []
        refs = [r for r in raw_refs if r in allowed_refs]
        if not topic or len(topic) > MAX_TOPIC_CHARS or not refs:
            warnings.append(f"{chapter_id} s{section_index}: dropped invalid quiz slot")
            return ""
        key = (topic, concept, tuple(sorted(refs)))
        if key in seen:
            warnings.append(f"{chapter_id} s{section_index}: dropped duplicate quiz slot")
            return ""
        if len(specs) >= max_slots:
            warnings.append(f"{chapter_id} s{section_index}: quiz cap reached, dropped extra")
            return ""
        seen.add(key)
        slot_id = f"{chapter_id}:s{section_index}:slot-{ordinal:03d}"
        ordinal += 1
        spec = SlotSpec(slot_id=slot_id, topic=topic, concept=concept, source_refs=refs)
        specs.append(spec)
        return _render_slot(spec)

    new_body = _STRAY_SLOT_RE.sub(_replace, body_md)
    if changed:
        new_body = re.sub(r"\n{3,}", "\n\n", new_body).strip() + "\n"
    return new_body, specs, warnings


# --- public API ------------------------------------------------------------------------


def sanitize_inline_quizzes(
    body_md: str,
    *,
    allowed_refs: set[str],
    chapter_id: str,
    section_index: int,
    max_items_per_block: int = MAX_ITEMS_PER_BLOCK,
    max_items_per_section: int = MAX_ITEMS_PER_SECTION,
) -> SanitizeResult:
    """Validate + canonicalize inline quizzes in ``body_md``; return new body and slot specs.

    Authored ``<QuizItem>``s are validated (answer ∈ choice ids, ≥2 choices, non-empty
    question/explanation, citations grounded in ``allowed_refs``) and re-rendered.
    ``<QuizItemSlot/>``s are validated (non-empty topic, sourceRefs grounded), deduped, and
    assigned canonical ids; their specs are returned for the application-quiz agent. Blocks
    are capped (``max_items_per_block`` per block, ``max_items_per_section`` per section),
    child order is preserved, and empty blocks are removed.
    """
    try:
        blocks = extract_inline_quizzes(body_md)
    except QuizExtractError as exc:
        # The section body may still contain MDX the chapter-level self-heal will repair
        # (e.g. a bare ``n<30`` comparison) which the strict parser rejects. We can't safely
        # canonicalize knowledge <QuizItem>s here (they need the AST), but we MUST still rescue
        # the application <QuizItemSlot/> specs by regex and stamp them with canonical ids —
        # otherwise the chapter silently loses those application quizzes (nothing re-sanitizes
        # after the chapter heal). Knowledge items stay as authored and are validated/canonical-
        # ized by the chapter-level MDX heal + check.
        LOGGER.warning(
            "%s s%s: inline-quiz sanitize fell back to regex (body not yet MDX-parseable): %s",
            chapter_id,
            section_index,
            exc,
        )
        new_body, rescued, fallback_warnings = _rescue_slots_via_regex(
            body_md,
            allowed_refs=allowed_refs,
            chapter_id=chapter_id,
            section_index=section_index,
            max_slots=max_items_per_section,
        )
        return SanitizeResult(body_md=new_body, slot_specs=rescued, warnings=fallback_warnings)
    warnings: list[str] = []
    slot_specs: list[SlotSpec] = []
    replacements: list[tuple[int, int, str]] = []
    section_count = 0
    slot_ordinal = 0
    seen_slot_keys: set[tuple[str, str, tuple[str, ...]]] = set()

    for block in blocks:
        kept: list[tuple[str, Any]] = []
        for child in block.get("children", []):
            if section_count >= max_items_per_section or len(kept) >= max_items_per_block:
                warnings.append(f"{chapter_id} s{section_index}: quiz cap reached, dropped extra")
                break
            kind = child.get("kind")
            if kind == "item":
                item = _valid_item(child, allowed_refs)
                if item is None:
                    warnings.append(f"{chapter_id} s{section_index}: dropped invalid quiz item")
                    continue
                item_id = f"quiz-{chapter_id}-s{section_index:03d}-{section_count + 1:02d}"
                kept.append(("item", _render_item(item, item_id)))
                section_count += 1
            elif kind == "slot":
                valid = _valid_slot(child, allowed_refs)
                if valid is None:
                    warnings.append(f"{chapter_id} s{section_index}: dropped invalid quiz slot")
                    continue
                topic, refs = valid
                concept = str(child.get("concept") or "").strip()
                key = (topic, concept, tuple(sorted(refs)))
                if key in seen_slot_keys:
                    warnings.append(f"{chapter_id} s{section_index}: dropped duplicate quiz slot")
                    continue
                seen_slot_keys.add(key)
                slot_id = f"{chapter_id}:s{section_index}:slot-{slot_ordinal:03d}"
                slot_ordinal += 1
                spec = SlotSpec(slot_id=slot_id, topic=topic, concept=concept, source_refs=refs)
                slot_specs.append(spec)
                kept.append(("slot", spec))
                section_count += 1
            else:
                warnings.append(
                    f"{chapter_id} s{section_index}: dropped stray <{child.get('name', '?')}>"
                )

        start, end = block.get("start"), block.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        replacements.append((start, end, _render_block(kept) if kept else ""))

    new_body = body_md
    for start, end, text in sorted(replacements, key=lambda r: r[0], reverse=True):
        new_body = new_body[:start] + text + new_body[end:]
    # collapse blank-line runs left by removed blocks
    new_body = re.sub(r"\n{3,}", "\n\n", new_body).strip() + "\n"
    return SanitizeResult(body_md=new_body, slot_specs=slot_specs, warnings=warnings)


def strip_inline_quizzes_and_control_slots(body_md: str) -> str:
    """Remove authored ``<QuizBlock>`` content and any ``<QuizItemSlot/>`` for downstream agents.

    Card/Summary/ApplicationQuiz agents must not see quiz tags as prose to echo. Blocks are
    located via the extractor (offsets) and removed; any stray slot outside a block is
    removed by regex as a backstop.
    """
    try:
        blocks = extract_inline_quizzes(body_md)
    except Exception:  # noqa: BLE001 - stripping is best-effort; never block downstream gen
        return _STRAY_SLOT_RE.sub("", body_md)
    spans = sorted(
        (
            (b["start"], b["end"])
            for b in blocks
            if isinstance(b.get("start"), int) and isinstance(b.get("end"), int)
        ),
        reverse=True,
    )
    stripped = body_md
    for start, end in spans:
        stripped = stripped[:start] + stripped[end:]
    stripped = _STRAY_SLOT_RE.sub("", stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip() + "\n"
