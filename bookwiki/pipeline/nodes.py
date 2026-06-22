from __future__ import annotations

import asyncio
import json
import re
import shutil
from html import escape, unescape
from pathlib import Path
from typing import Any

import yaml

from bookwiki.agents import (
    ApplicationQuizAgent,
    CardAgent,
    ChapterMdxEditRepairAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptContentRewriteAgent,
    ConceptExtractAgent,
    ConceptMdxEditRepairAgent,
    ConceptReconcileAgent,
    ReviewAgent,
    SectionAgent,
    SkeletonAgent,
    SourceLayoutRepairAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
    VisionCaptionAgent,
)
from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.checkers.mdx_validator import mdx_validator_available, validate_mdx
from bookwiki.checkers.quiz_extractor import QuizExtractError, extract_inline_quizzes
from bookwiki.convert.common import (
    BOOK_FIGURE_TAG_RE,
    parse_book_figure_tag,
    source_id_from_stem,
)
from bookwiki.convert.mineru_client import convert_document_to_source
from bookwiki.convert.source_normalizer import (
    NormalizedSource,
    SourceBlock,
    _render_figure,
    normalize_structured_source,
)
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.generate.sections import _body_too_short, generate_chapter_sections
from bookwiki.generate.validate_artifact import ArtifactIssue, validate_artifact
from bookwiki.indexer.sqlite_builder import build_sqlite_index
from bookwiki.integrator.markdown_renderers import (
    convert_html_style_attrs,
    normalize_citation_quote_math,
    normalize_mdx_math,
    normalize_source_cites,
)
from bookwiki.integrator.stitching import audit_stitching
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.concept import ConceptReconciledItem, ConceptReconcileResult, ConceptResult
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.split.chapter_splitter import parse_approved_structure
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_file, sha256_text
from bookwiki.utils.logging import get_logger

State = dict[str, Any]

_LOG = get_logger(__name__)

APPROVED_STRUCTURE_MARKER = "# bookwiki: approved-structure"
PENDING_STRUCTURE_MARKER = "# bookwiki: pending-structure-review"
CONVERT_ARTIFACT_VERSION = 2


def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _json_model(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)


def _agent_result_payload(agent_cls: type[Any], model: str, result: Any) -> dict[str, Any]:
    payload = _json_model(result)
    return {
        "_schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "_agent": agent_cls.__name__,
        "_model": model,
        "result": payload,
    }


def _agent_result(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result")
    return result if isinstance(result, dict) else data


def _read_all_markdown(paths: list[Path]) -> str:
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)


def _chapter_titles(approved_structure: str) -> list[tuple[str, str]]:
    return [
        (chapter.chapter_id, chapter.title)
        for chapter in parse_approved_structure(approved_structure)
    ]


def _chapter_topics(approved_structure: str) -> dict[str, list[str]]:
    try:
        chapters = parse_approved_structure(approved_structure)
    except ValueError:
        return {}
    return {chapter.chapter_id: list(chapter.topics) for chapter in chapters}


def _source_figures(source_md: str) -> list[dict[str, str]]:
    """Extract the de-duplicated ``<BookFigure/>`` references found in ``source_md``.

    Figures are returned in first-seen order keyed by ``id``; captions are
    ``html.unescape``-d so the agent prompt receives human-readable text.
    """
    figures: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in BOOK_FIGURE_TAG_RE.findall(source_md):
        attrs = parse_book_figure_tag(tag)
        figure_id = unescape(attrs.get("id", ""))
        if not figure_id or figure_id in seen:
            continue
        seen.add(figure_id)
        figure: dict[str, str] = {"id": figure_id}
        caption = attrs.get("caption")
        if caption:
            figure["caption"] = unescape(caption)
        figures.append(figure)
    return figures


def _pending_approved_structure_text(proposed_structure: str) -> str:
    return (
        f"{PENDING_STRUCTURE_MARKER}\n"
        "# Review this file, edit chapters/topics/source_refs as needed, then replace\n"
        f"# the first marker with `{APPROVED_STRUCTURE_MARKER}` before running split.\n"
        f"{proposed_structure.rstrip()}\n"
    )


def _assert_structure_approved(approved_structure: str) -> None:
    if any(line.strip() == APPROVED_STRUCTURE_MARKER for line in approved_structure.splitlines()):
        return
    msg = (
        "approved-structure.yaml is not marked as reviewed. Review "
        "work/structure/proposed-structure.yaml, edit work/structure/approved-structure.yaml, "
        f"then add a line exactly `{APPROVED_STRUCTURE_MARKER}` before running split."
    )
    raise ValueError(msg)


def _cache_dir(cfg: BookConfig) -> Path:
    return cfg.cache_dir / "tasks"


def _stage_cache_hit(results: list[CacheResult]) -> bool:
    return bool(results) and all(item.cache_hit for item in results)


def _safe_file_stem(value: str, *, fallback_prefix: str = "item") -> str:
    normalized = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-.")
    if normalized:
        return normalized
    return f"{fallback_prefix}-{sha256_text(value)[:8]}"


def _unique_file_stem(value: str, used: set[str], *, fallback_prefix: str = "item") -> str:
    stem = _safe_file_stem(value, fallback_prefix=fallback_prefix)
    candidate = stem
    if candidate in used:
        digest = sha256_text(value)[:8]
        candidate = f"{stem}-{digest}"
        counter = 2
        while candidate in used:
            candidate = f"{stem}-{digest}-{counter}"
            counter += 1
    used.add(candidate)
    return candidate


def _clear_generated_files(directory: Path, pattern: str) -> None:
    for path in directory.glob(pattern):
        if path.is_file():
            path.unlink()


def _source_file_metadata(path: Path, cfg: BookConfig, source_sha256: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": _rel(path, cfg.book_dir),
        "sha256": source_sha256,
        "size_bytes": stat.st_size,
    }


def _outputs_metadata(out_path: Path, cfg: BookConfig, body: str) -> dict[str, Any]:
    return {
        "markdown_path": _rel(out_path, cfg.book_dir),
        "markdown_sha256": sha256_text(body),
    }


def _attach_convert_metadata(
    manifest: dict[str, Any],
    *,
    source_path: Path,
    source_sha256: str,
    out_path: Path,
    body: str,
    cfg: BookConfig,
) -> dict[str, Any]:
    return {
        **manifest,
        "convert_artifact_version": CONVERT_ARTIFACT_VERSION,
        "source_file": _source_file_metadata(source_path, cfg, source_sha256),
        "outputs": _outputs_metadata(out_path, cfg, body),
    }


def _matching_convert_artifact(
    *,
    source_path: Path,
    source_sha256: str,
    out_path: Path,
    manifest_path: Path,
    cfg: BookConfig,
) -> bool:
    if not out_path.exists() or not manifest_path.exists():
        return False
    manifest = read_json(manifest_path, default={})
    if not isinstance(manifest, dict):
        return False
    source_file = manifest.get("source_file")
    outputs = manifest.get("outputs")
    if manifest.get("convert_artifact_version") != CONVERT_ARTIFACT_VERSION:
        return False
    if not isinstance(source_file, dict) or not isinstance(outputs, dict):
        return False
    if source_file.get("path") != _rel(source_path, cfg.book_dir):
        return False
    if source_file.get("sha256") != source_sha256:
        return False
    expected_markdown_sha256 = outputs.get("markdown_sha256")
    if not isinstance(expected_markdown_sha256, str) or not expected_markdown_sha256:
        return False
    try:
        body = out_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return sha256_text(body) == expected_markdown_sha256


def _mdx_prop(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _citation_items(citations: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"ref_id": str(item.get("ref_id", "")), "quote": str(item.get("quote", ""))}
        for item in citations
    ]


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


def _display_chapter_title(chapter_id: str, title: str) -> str:
    # The chapter title is the verbatim free-form name; the id is just a slug derived from it.
    # No "Chapter N" prefix is synthesised — a title that wants one already contains it.
    return str(title).strip() or str(chapter_id)


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
    answer_id = next(
        (
            _choice_id(choice_index)
            for choice_index, choice in enumerate(choices, start=1)
            if choice.strip() == answer
        ),
        _choice_id(1),
    )
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


def _document_title(body: str, fallback: str) -> str:
    frontmatter = re.match(r"^---\n(.*?)\n---", body, flags=re.DOTALL)
    if frontmatter:
        for line in frontmatter.group(1).splitlines():
            if line.startswith("title:"):
                return line.split(":", 1)[1].strip().strip('"')
    return next(
        (line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("#")),
        fallback,
    )


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
_QUIZ_ITEM_SLOT_RE = re.compile(r"<QuizItemSlot\b[^>]*/>")
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


def _resolve_item_slots(body_md: str, quiz: dict[str, Any]) -> str:
    """Replace each inline ``<QuizItemSlot id=X/>`` with its filled application ``<QuizItem>``.

    Knowledge quizzes are already authored inline in ``body_md``; only application slots need
    filling. Items are matched to slots by canonical ``slot_id``. A slot with no matching item
    (the agent produced fewer, or repair dropped it) is removed, and a ``<QuizBlock>`` left
    empty is removed too. An item carrying no ``slot_id`` is a stale ``after_block``-era
    artifact and is a hard error (regenerate the chapter).
    """
    items_by_slot: dict[str, tuple[dict[str, Any], int]] = {}
    for index, item in enumerate(quiz.get("items", []), start=1):
        if not isinstance(item, dict):
            continue
        slot_id = str(item.get("slot_id") or "")
        if not slot_id:
            raise ValueError(
                "quiz item has no slot_id (stale after_block artifact; regenerate): "
                f"{str(item.get('question', ''))[:60]}"
            )
        items_by_slot[slot_id] = (item, index)

    def _replace(match: re.Match[str]) -> str:
        id_match = re.search(r'id="([^"]*)"', match.group(0))
        entry = items_by_slot.get(id_match.group(1) if id_match else "")
        if entry is None:
            return ""
        item, index = entry
        return _quiz_item_mdx(item, index)

    resolved = _QUIZ_ITEM_SLOT_RE.sub(_replace, body_md)
    return _EMPTY_QUIZ_BLOCK_RE.sub("", resolved)


def _inline_quiz_answer_issues(text: str, stem: str) -> list[Issue]:
    """Warn if a rendered inline quiz item's answer is not among its choice ids.

    Generate-time ``sanitize_inline_quizzes`` already enforces this on authored knowledge
    quizzes; this is a macro-stage safety net for residue (e.g. a section whose body was not
    MDX-parseable when sanitized and was only healed later). Emitted as a ``warning`` because
    there is no macro repair path for inline (body-authored) quizzes — it surfaces in the
    check report without wedging the repair loop.
    """
    try:
        blocks = extract_inline_quizzes(text)
    except QuizExtractError:
        return []  # a real parse failure is already reported as MDX_PARSE_ERROR
    issues: list[Issue] = []
    for block in blocks:
        for child in block.get("children", []):
            if child.get("kind") != "item":
                continue
            choice_ids = {choice.get("id") for choice in child.get("choices", [])}
            if child.get("answer") not in choice_ids:
                issues.append(
                    Issue(
                        severity="warning",
                        code="INLINE_QUIZ_ANSWER_NOT_IN_CHOICES",
                        message=f"{stem}.mdx inline quiz answer is not among its choices",
                        owner_task_id=f"{stem}:quiz",
                    )
                )
    return issues


def _normalize_concept_links(
    markdown: str,
    alias_map: dict[str, str],
    concept_previews: dict[str, dict[str, str]],
    chapter_previews: dict[str, dict[str, str]] | None = None,
) -> str:
    markdown, fence_stash = _stash_code_fences(markdown)
    markdown, quiz_stash = _stash_quiz_blocks(markdown)
    linked_canonicals: set[str] = set()
    chapters = chapter_previews or {}

    def replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        canonical = alias_map.get(label) or alias_map.get(_concept_key(label)) or label
        preview = concept_previews.get(canonical)
        if preview:
            linked_canonicals.add(canonical)
            # A concept always wins a name it shares with a chapter; record the collision so the
            # author can disambiguate (e.g. give the chapter a more specific title).
            if label in chapters or _concept_key(label) in chapters:
                _LOG.warning(
                    "AMBIGUOUS_WIKILINK label=%r resolves to both a concept and a chapter; "
                    "concept wins",
                    label,
                )
            return _preview_link_mdx(
                preview["href"], preview["title"], preview["summary"], canonical
            )
        # No concept matched: fall back to a chapter-to-chapter link by (exact or normalized) title.
        chapter = chapters.get(label) or chapters.get(_concept_key(label))
        if chapter:
            return _preview_link_mdx(
                chapter["href"], chapter["title"], chapter["summary"], chapter["title"]
            )
        return f"[[{canonical}]]"

    normalized = re.sub(r"\[\[([^\]]+)\]\]", replace, markdown)
    linked = _auto_link_concept_terms(
        normalized, _concept_link_terms(alias_map, concept_previews), linked_canonicals
    )
    return _unstash_code_fences(_unstash_quiz_blocks(linked, quiz_stash), fence_stash)


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


def _auto_link_concept_terms(
    markdown: str,
    terms: list[tuple[str, str, dict[str, str]]],
    linked_canonicals: set[str],
) -> str:
    if not terms:
        return markdown
    lines: list[str] = []
    for line in markdown.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            lines.append(line)
        else:
            lines.append(_auto_link_concept_terms_in_line(line, terms, linked_canonicals))
    return "".join(lines)


_CONCEPT_LINK_PROTECTED_RE = re.compile(
    r"(```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\[[^\]\n]+\]\([^)]+\)|<[^>\n]+>)"
)


def _auto_link_concept_terms_in_line(
    line: str,
    terms: list[tuple[str, str, dict[str, str]]],
    linked_canonicals: set[str],
) -> str:
    parts = _CONCEPT_LINK_PROTECTED_RE.split(line)
    return "".join(
        part
        if _CONCEPT_LINK_PROTECTED_RE.fullmatch(part)
        else _auto_link_concept_terms_in_text(part, terms, linked_canonicals)
        for part in parts
    )


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


def _concept_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _suspicious_phrases(markdown: str) -> list[str]:
    phrases = ["ignore previous instructions", "system prompt", "developer message"]
    lower = markdown.lower()
    return [phrase for phrase in phrases if phrase in lower]


def _mdx_link_exists(base_dir: Path, target: str) -> bool:
    clean = target.split("#", 1)[0]
    if not clean:
        return True
    path = (base_dir / clean).resolve()
    candidates = [path]
    if path.suffix == "":
        candidates.extend([path.with_suffix(".mdx"), path / "index.mdx"])
    return any(candidate.exists() for candidate in candidates)


def _allowed_source_refs(state: State, cfg: BookConfig) -> set[str]:
    refs: set[str] = set()
    for rel_path in state.get("sources_md", []):
        path = cfg.book_dir / rel_path
        if path.exists():
            refs.update(re.findall(r"source_ref:\s*([^\s>]+)", path.read_text(encoding="utf-8")))
    if refs:
        return refs
    for paths in state.get("agent_results", {}).values():
        for rel_path in paths.values():
            refs.update(_iter_citation_refs(read_json(cfg.book_dir / rel_path)))
    return refs


def _iter_citation_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        if "ref_id" in value and "quote" in value:
            refs.append(str(value["ref_id"]))
        for item in value.values():
            refs.extend(_iter_citation_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_iter_citation_refs(item))
    return refs


def _render_check_report_md(report: CheckReport) -> str:
    lines = ["# Check Report", "", f"Status: `{report.status}`", ""]
    if not report.issues:
        lines.append("No issues.")
        return "\n".join(lines) + "\n"
    for issue in report.issues:
        lines.append(
            f"- `{issue.severity}` `{issue.code}` owner `{issue.owner_task_id}`: {issue.message}"
        )
    return "\n".join(lines) + "\n"


def _owner_output_payload(owner_task_id: str, state: State, cfg: BookConfig) -> dict[str, Any]:
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return {}
    return read_json(path)


def _owner_artifact_path(owner_task_id: str, state: State, cfg: BookConfig) -> Path | None:
    if owner_task_id.startswith("concept:"):
        for name, rel_path in state.get("concept_pages", {}).items():
            path = cfg.book_dir / rel_path
            payload = read_json(path, default={})
            owner = str(_agent_result(payload).get("owner_task_id") or f"concept:{name}")
            if owner == owner_task_id:
                return path
        return None
    chapter_id, _, kind = owner_task_id.partition(":")
    rel_path = state.get("agent_results", {}).get(chapter_id, {}).get(kind)
    return cfg.book_dir / rel_path if rel_path else None


def _artifact_owner_task_id(ch_id: str, kind: str, payload: dict[str, Any]) -> str:
    result = _agent_result(payload)
    owner = result.get("owner_task_id")
    return str(owner) if owner else f"{ch_id}:{kind}"


def _concept_contexts(item: dict[str, Any], state: State, cfg: BookConfig) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for ch_id in item.get("source_chapter_ids", []):
        chapter_id_text = str(ch_id)
        paths = state.get("agent_results", {}).get(chapter_id_text, {})
        chapter = (
            _agent_result(read_json(cfg.book_dir / paths["chapter"]))
            if paths.get("chapter")
            else {}
        )
        summary = (
            _agent_result(read_json(cfg.book_dir / paths["summary"]))
            if paths.get("summary")
            else {}
        )
        source_rel = state.get("chapter_sources", {}).get(chapter_id_text)
        source_md = ""
        if source_rel:
            source_path = cfg.book_dir / source_rel
            if source_path.exists():
                source_md = source_path.read_text(encoding="utf-8")
        contexts.append(
            {
                "chapter_id": chapter_id_text,
                "title": _display_chapter_title(
                    chapter_id_text, str(chapter.get("title", chapter_id_text))
                ),
                "body_md": chapter.get("body_md", ""),
                "summary_md": summary.get("summary_md", ""),
                "source_md": source_md,
                "citations": [
                    *_citation_items(chapter.get("citations", [])),
                    *_citation_items(summary.get("citations", [])),
                ],
            }
        )
    return contexts


def _apply_repair(
    owner_task_id: str, issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> dict[str, Any] | None:
    """Apply deterministic repairs, preferring DROP over fabrication.

    Returns an audit record of what was removed (or ``None`` if nothing changed).
    Earlier this collapsed every invalid citation ref onto one valid ref and rewrote
    quiz answers / empty cards with placeholder text - both silently corrupted
    content (wrong attribution, wrong answers, English stubs in a zh-CN book). We now
    delete the offending citation/quiz-item/card instead, so the artifact stays
    truthful and the loss is recorded for review.
    """
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return None
    payload = read_json(path)
    result = payload.get("result", payload)
    codes = {str(issue.get("code")) for issue in issues}
    allowed_refs = _allowed_source_refs(state, cfg)
    actions: dict[str, Any] = {}
    if "UNKNOWN_SOURCE_REF" in codes and allowed_refs:
        dropped = _drop_invalid_citations(result, allowed_refs)
        if dropped:
            actions["dropped_citations"] = dropped
    _, _, kind = owner_task_id.partition(":")
    if kind == "quiz" and "QUIZ_ANSWER_NOT_IN_CHOICES" in codes:
        dropped_quiz = _drop_invalid_quiz_items(result)
        if dropped_quiz:
            actions["dropped_quiz_items"] = dropped_quiz
    elif kind == "card" and "EMPTY_CARD_SIDE" in codes:
        dropped_cards = _drop_empty_cards(result)
        if dropped_cards:
            actions["dropped_cards"] = dropped_cards
    write_json(path, payload)
    if actions:
        return {"owner_task_id": owner_task_id, **actions}
    return None


def _is_invalid_citation(elem: Any, allowed_refs: set[str]) -> bool:
    return (
        isinstance(elem, dict)
        and "ref_id" in elem
        and "quote" in elem
        and str(elem["ref_id"]) not in allowed_refs
    )


def _drop_invalid_citations(value: Any, allowed_refs: set[str]) -> list[str]:
    """Recursively remove citation dicts whose ``ref_id`` is not allowed.

    Returns the list of removed ``ref_id`` values. Unlike the previous
    collapse-to-first-ref behaviour, this never reassigns a citation to a different
    (wrong) source - an unverifiable citation is dropped, not silently re-attributed.
    """
    removed: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in list(node.items()):
                if isinstance(item, list):
                    kept = [elem for elem in item if not _is_invalid_citation(elem, allowed_refs)]
                    removed.extend(
                        str(elem["ref_id"])
                        for elem in item
                        if _is_invalid_citation(elem, allowed_refs)
                    )
                    node[key] = kept
                    for elem in kept:
                        walk(elem)
                else:
                    walk(item)
        elif isinstance(node, list):
            for elem in node:
                walk(elem)

    walk(value)
    return removed


def _drop_invalid_quiz_items(result: dict[str, Any]) -> list[str]:
    """Drop quiz items whose answer is not among the choices.

    Returns short descriptions of the removed items. ``QuizResult.items`` has no
    minimum length, so deleting down to zero is schema-valid.
    """
    items = result.get("items", [])
    kept_items: list[Any] = []
    removed: list[str] = []
    for item in items:
        choices = [str(choice) for choice in item.get("choices", [])]
        if bool(choices) and str(item.get("answer", "")) not in choices:
            removed.append(str(item.get("question", ""))[:60])
            continue
        kept_items.append(item)
    if removed:
        result["items"] = kept_items
    return removed


def _drop_empty_cards(result: dict[str, Any]) -> list[str]:
    """Drop cards with an empty front or back. ``CardResult.items`` has no minimum."""
    items = result.get("items", [])
    kept: list[Any] = []
    removed: list[str] = []
    for index, item in enumerate(items, start=1):
        if not str(item.get("front", "")).strip() or not str(item.get("back", "")).strip():
            removed.append(f"card {index}")
            continue
        kept.append(item)
    if removed:
        result["items"] = kept
    return removed


async def convert_node(state: State, cfg: BookConfig) -> State:
    input_files = sorted(path for path in cfg.input_dir.iterdir() if path.is_file())
    if not input_files:
        msg = f"no input files found in {cfg.input_dir}"
        raise FileNotFoundError(msg)

    out_dir = ensure_dir(cfg.work_dir / "sources_md")
    manifest_dir = ensure_dir(cfg.work_dir / "source_refs")
    outputs: list[str] = []
    manifests: list[str] = []
    for path in input_files:
        source_id = source_id_from_stem(path.stem)
        out_path = out_dir / f"{source_id}.md"
        manifest_path = manifest_dir / f"{source_id}.json"
        source_sha256 = sha256_file(path)
        if _matching_convert_artifact(
            source_path=path,
            source_sha256=source_sha256,
            out_path=out_path,
            manifest_path=manifest_path,
            cfg=cfg,
        ):
            outputs.append(_rel(out_path, cfg.book_dir))
            manifests.append(_rel(manifest_path, cfg.book_dir))
            continue
        suffix = path.suffix.lower()
        if suffix in {".pdf", ".pptx"}:
            parsed = convert_document_to_source(path, source_id=source_id)
            _materialize_mineru_assets(parsed, source_id, cfg)
            normalized = await _normalize_with_layout_repair(parsed, source_id, cfg)
            body = normalized.markdown
            manifest = normalized.manifest
        elif suffix in {".txt", ".md"}:
            body = convert_text_to_md(path, source_id=source_id)
            normalized = normalize_structured_source(raw_md=body, source_id=source_id)
            manifest = normalized.manifest
        else:
            msg = f"unsupported source file type: {path.name}"
            raise ValueError(msg)
        manifest = _attach_convert_metadata(
            manifest,
            source_path=path,
            source_sha256=source_sha256,
            out_path=out_path,
            body=body,
            cfg=cfg,
        )
        write_text(out_path, body)
        write_json(manifest_path, manifest)
        outputs.append(_rel(out_path, cfg.book_dir))
        manifests.append(_rel(manifest_path, cfg.book_dir))

    return {"sources_md": outputs, "source_ref_manifests": manifests}


async def caption_node(state: State, cfg: BookConfig) -> State:
    source_mds = [str(path) for path in state.get("sources_md") or []]
    manifests = [str(path) for path in state.get("source_ref_manifests") or []]
    if not source_mds:
        msg = "caption requires converted markdown; run convert first"
        raise FileNotFoundError(msg)
    if not manifests:
        msg = "caption requires source ref manifests; run convert first"
        raise FileNotFoundError(msg)

    md_by_source_id = {Path(path).stem: str(path) for path in source_mds}
    caption_results: list[dict[str, Any]] = []
    cache_results: list[CacheResult] = []
    caption_failures: list[str] = []
    settings = _vision_caption_settings(cfg)

    for manifest_rel in manifests:
        manifest_path = cfg.book_dir / manifest_rel
        if not manifest_path.exists():
            msg = f"caption source ref manifest not found: {manifest_path}"
            raise FileNotFoundError(msg)
        manifest = read_json(manifest_path, default={})
        if not isinstance(manifest, dict):
            msg = f"caption source ref manifest is not a JSON object: {manifest_path}"
            raise ValueError(msg)
        source_id = str(manifest.get("source_id") or Path(manifest_rel).stem)
        md_rel = md_by_source_id.get(source_id) or md_by_source_id.get(Path(manifest_rel).stem)
        md_path = cfg.book_dir / md_rel if md_rel else None
        if md_path is None or not md_path.exists():
            msg = f"caption converted markdown not found for {manifest_rel}"
            raise FileNotFoundError(msg)
        md_text = md_path.read_text(encoding="utf-8")
        warnings = [
            str(item)
            for item in manifest.get("vision_warnings", [])
            if isinstance(item, str) and item.strip()
        ]

        if settings["mode"] == "off":
            continue

        normalized = NormalizedSource(markdown=md_text, manifest=manifest)
        candidates = _image_caption_candidates(normalized)[: settings["max_images"]]
        jobs = [_vision_caption_job(candidate, md_text) for candidate in candidates]
        outcomes = await _run_vision_caption_jobs(
            jobs,
            cfg,
            max_concurrent=settings["max_concurrent"],
        )
        for job, outcome in zip(jobs, outcomes, strict=False):
            candidate = job["candidate"]
            if isinstance(outcome, Exception):
                warning = f"vision caption failed for {candidate['block_id']}: {outcome}"
                warnings.append(warning)
                caption_failures.append(warning)
                continue
            result = outcome

            block = _set_manifest_block_caption(
                manifest,
                candidate["block_id"],
                result.result.caption_md,
                model=cfg.model_for("vision"),
            )
            if block is None:
                warnings.append(
                    f"vision caption target block not found for {candidate['block_id']}"
                )
                continue
            if md_text:
                md_text, replaced = _replace_book_figure(md_text, block)
                if not replaced:
                    warnings.append(
                        f"vision caption markdown tag not found for {candidate['block_id']}"
                    )
            cache_results.append(result)
            caption_results.append(
                {
                    "block_id": candidate["block_id"],
                    "source_ref": candidate["source_ref"],
                    "manifest": manifest_rel,
                    "cache_hit": result.cache_hit,
                }
            )

        if warnings:
            manifest["vision_warnings"] = warnings
        write_json(manifest_path, manifest)
        if md_path and md_text:
            write_text(md_path, md_text)

    if caption_failures:
        count = len(caption_failures)
        noun = "image" if count == 1 else "images"
        details = "; ".join(caption_failures[:3])
        if count > 3:
            details += f"; ... {count - 3} more"
        msg = f"caption failed for {count} {noun}: {details}"
        raise RuntimeError(msg)

    return {
        "sources_md": source_mds,
        "source_ref_manifests": manifests,
        "caption_results": caption_results,
        "cache_hit": not cache_results or _stage_cache_hit(cache_results),
    }


async def _normalize_with_layout_repair(
    parsed: dict[str, Any], source_id: str, cfg: BookConfig
) -> NormalizedSource:
    settings = _source_layout_repair_settings(cfg)
    normalized = normalize_structured_source(
        raw_md=str(parsed.get("markdown") or ""),
        source_id=source_id,
        content_list_v2=parsed.get("content_list_v2"),
        content_list=parsed.get("content_list"),
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
    )
    if settings["mode"] == "off" or not normalized.repair_candidates:
        return normalized

    result = await run_with_cache(
        SourceLayoutRepairAgent,
        {
            "source_id": source_id,
            "candidates": normalized.repair_candidates,
            "manifest": normalized.manifest,
        },
        model=cfg.model_for("source_layout_repair"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    patches = [
        patch.model_dump(mode="json")
        for patch in result.result.patches
        if patch.confidence >= settings["min_confidence"]
    ]
    if not patches:
        return normalized
    repaired = normalize_structured_source(
        raw_md=str(parsed.get("markdown") or ""),
        source_id=source_id,
        content_list_v2=parsed.get("content_list_v2"),
        content_list=parsed.get("content_list"),
        repair_patches=patches,
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
    )
    return repaired


def _materialize_mineru_assets(parsed: dict[str, Any], source_id: str, cfg: BookConfig) -> None:
    assets = [asset for asset in parsed.get("assets") or [] if isinstance(asset, dict)]
    if not assets:
        return
    asset_dir = ensure_dir(cfg.work_dir / "assets" / source_id)
    path_index: dict[str, str] = {}
    for index, asset in enumerate(assets, start=1):
        data = asset.get("data")
        if not isinstance(data, bytes):
            continue
        filename = _safe_asset_filename(str(asset.get("filename") or ""), index)
        out_path = asset_dir / filename
        out_path.write_bytes(data)
        rel_path = _rel(out_path, cfg.book_dir)
        archive_path = str(asset.get("archive_path") or filename).replace("\\", "/")
        for key in {archive_path, archive_path.lower(), Path(archive_path).name.lower()}:
            path_index[key] = rel_path
    if path_index:
        for value in (parsed.get("content_list_v2"), parsed.get("content_list")):
            _attach_asset_paths(value, path_index)


def _attach_asset_paths(value: Any, path_index: dict[str, str]) -> None:
    if isinstance(value, list):
        for item in value:
            _attach_asset_paths(item, path_index)
        return
    if not isinstance(value, dict):
        return
    block_type = str(value.get("type") or value.get("category") or "").lower()
    if block_type in {"image", "chart"} and not value.get("asset_path"):
        for raw in _asset_path_refs(value):
            rel_path = _asset_match(raw, path_index)
            if rel_path:
                value["asset_path"] = rel_path
                break
    for item in value.values():
        _attach_asset_paths(item, path_index)


def _asset_path_refs(value: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("img_path", "image_path", "path", "url"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            refs.append(raw)
    content = value.get("content")
    if isinstance(content, dict):
        image_source = content.get("image_source") or content.get("chart_source")
        if isinstance(image_source, dict):
            for key in ("img_path", "image_path", "path", "url"):
                raw = image_source.get(key)
                if isinstance(raw, str) and raw.strip():
                    refs.append(raw)
    return refs


def _asset_match(raw_path: str, path_index: dict[str, str]) -> str | None:
    normalized = raw_path.replace("\\", "/").lower().lstrip("/")
    if normalized in path_index:
        return path_index[normalized]
    basename = Path(normalized).name
    if basename in path_index:
        return path_index[basename]
    for key, rel_path in path_index.items():
        if key.endswith(normalized) or key.endswith("/" + basename):
            return rel_path
    return None


def _safe_asset_filename(filename: str, index: int) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(filename).name).strip(".-")
    if not clean:
        clean = f"asset-{index:03d}.png"
    return clean


def _vision_caption_job(candidate: dict[str, Any], md_text: str) -> dict[str, Any]:
    candidate_input = dict(candidate)
    section_window = _section_context_window_for_book_figure(md_text, candidate["block_id"])
    section_span = None
    if section_window is not None:
        candidate_input["section_context"] = section_window["text"]
        section_span = section_window["span"]
    return {
        "candidate": candidate,
        "input": candidate_input,
        "section_span": section_span,
        "source_ref": str(candidate.get("source_ref") or ""),
    }


async def _run_vision_caption_jobs(
    jobs: list[dict[str, Any]], cfg: BookConfig, *, max_concurrent: int
) -> list[CacheResult | Exception]:
    outcomes: list[CacheResult | Exception | None] = [None] * len(jobs)
    indexed_jobs = [{**job, "index": index} for index, job in enumerate(jobs)]
    groups = _caption_conflict_groups(indexed_jobs)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_one(job: dict[str, Any]) -> CacheResult | Exception:
        async with semaphore:
            try:
                return await _run_vision_caption(job["input"], cfg)
            except Exception as exc:  # noqa: BLE001 - captioning is best-effort enrichment
                return exc

    async def run_group(group: dict[str, Any]) -> None:
        for job in sorted(group["jobs"], key=lambda item: int(item["index"])):
            outcomes[int(job["index"])] = await run_one(job)

    await asyncio.gather(*(run_group(group) for group in groups))
    return [
        item if item is not None else RuntimeError("caption job did not run") for item in outcomes
    ]


def _caption_conflict_groups(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for job in jobs:
        span = job.get("section_span")
        if span is None:
            groups.append(
                {
                    "section_span": None,
                    "source_ref": str(job.get("source_ref") or ""),
                    "jobs": [job],
                }
            )
            continue
        span = tuple(span)
        source_ref = str(job.get("source_ref") or "")
        merged_jobs = [job]
        remaining = groups
        changed = True
        while changed:
            changed = False
            next_remaining = []
            for group in remaining:
                group_span = group.get("section_span")
                same_source_ref = str(group.get("source_ref") or "") == source_ref
                if (
                    group_span is not None
                    and same_source_ref
                    and _spans_overlap(span, tuple(group_span))
                ):
                    span = _union_spans(span, tuple(group_span))
                    merged_jobs.extend(group["jobs"])
                    changed = True
                else:
                    next_remaining.append(group)
            remaining = next_remaining
        groups = [
            *remaining,
            {"section_span": span, "source_ref": source_ref, "jobs": merged_jobs},
        ]
    return groups


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _union_spans(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return min(left[0], right[0]), max(left[1], right[1])


async def _run_vision_caption(candidate: dict[str, Any], cfg: BookConfig) -> CacheResult:
    agent_input = _vision_caption_agent_input(candidate, cfg)
    return await run_with_cache(
        VisionCaptionAgent,
        agent_input,
        model=cfg.model_for("vision"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )


def _set_manifest_block_caption(
    manifest: dict[str, Any], block_id: str, caption: str, *, model: str
) -> dict[str, Any] | None:
    block = _manifest_block(manifest, block_id)
    if block is None:
        return None
    block["caption"] = caption
    block["caption_source"] = "vision"
    block["caption_model"] = model
    return block


def _manifest_block(manifest: dict[str, Any], block_id: str) -> dict[str, Any] | None:
    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks", []):
            if isinstance(block, dict) and str(block.get("block_id") or "") == block_id:
                return block
    return None


def _replace_book_figure(markdown: str, block: dict[str, Any]) -> tuple[str, bool]:
    figure = _render_figure(_source_block_from_manifest(block), "")
    if not figure:
        return markdown, False
    pattern = _book_figure_pattern(str(block.get("block_id") or ""))
    if not pattern.search(markdown):
        return markdown, False
    return pattern.sub(lambda _match: figure, markdown, count=1), True


def _section_context_for_book_figure(markdown: str, block_id: str) -> str:
    window = _section_context_window_for_book_figure(markdown, block_id)
    return str(window["text"]) if window is not None else ""


def _section_context_window_for_book_figure(markdown: str, block_id: str) -> dict[str, Any] | None:
    match = _book_figure_pattern(block_id).search(markdown)
    if not match:
        return None
    start = 0
    end = len(markdown)
    for heading in re.finditer(r"(?m)^#{1,6}\s+\S.*$", markdown):
        if heading.start() <= match.start():
            start = heading.start()
            continue
        end = heading.start()
        break
    return {
        "text": markdown[start:end].strip(),
        "span": (start, end),
    }


def _book_figure_pattern(block_id: str) -> re.Pattern[str]:
    escaped = re.escape(block_id)
    return re.compile(rf'<BookFigure\b(?=[^>]*\bid="{escaped}")[^>]*/>')


def _source_block_from_manifest(block: dict[str, Any]) -> SourceBlock:
    return SourceBlock(
        block_id=str(block.get("block_id") or ""),
        page_ref=str(block.get("page_ref") or ""),
        page_idx=_int_setting(block.get("page_idx"), 0),
        block_index=_int_setting(block.get("block_index"), 0),
        type=str(block.get("type") or "image"),
        text=str(block.get("text_preview") or ""),
        bbox=block.get("bbox") if isinstance(block.get("bbox"), list) else None,
        asset_path=str(block.get("asset_path") or "") or None,
        caption=str(block.get("caption") or "") or None,
    )


def _image_caption_candidates(normalized: NormalizedSource) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page in normalized.manifest.get("pages", []):
        blocks = page.get("blocks", []) if isinstance(page, dict) else []
        nearby_text = " ".join(
            str(block.get("text_preview") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") not in {"image", "chart"}
        )
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in {"image", "chart"}:
                continue
            if not block.get("asset_path"):
                continue
            if str(block.get("caption_source") or "").lower() == "vision":
                continue
            existing_caption = str(block.get("caption") or "").strip()
            candidates.append(
                {
                    "block_id": str(block.get("block_id") or ""),
                    "source_ref": str(block.get("page_ref") or page.get("source_ref") or ""),
                    "asset_path": str(block.get("asset_path") or ""),
                    "existing_caption": existing_caption,
                    "nearby_text": nearby_text,
                    "bbox": block.get("bbox"),
                }
            )
    return candidates


def _vision_caption_agent_input(candidate: dict[str, Any], cfg: BookConfig) -> dict[str, Any]:
    agent_input = dict(candidate)
    image_path = Path(str(candidate.get("asset_path") or ""))
    if not image_path.is_absolute():
        image_path = cfg.book_dir / image_path
    agent_input["asset_full_path"] = str(image_path)
    if image_path.is_file():
        agent_input["asset_sha256"] = sha256_file(image_path)
    return agent_input


def _vision_caption_settings(cfg: BookConfig) -> dict[str, Any]:
    raw = cfg.generation.get("visionCaption")
    settings = raw if isinstance(raw, dict) else {}
    mode = str(settings.get("mode", "auto")).lower()
    if mode not in {"auto", "off"}:
        mode = "auto"
    return {
        "mode": mode,
        "max_images": _int_setting(settings.get("maxImagesPerSource"), 20),
        "max_concurrent": _int_setting(settings.get("maxConcurrent"), 10),
    }


def _source_layout_repair_settings(cfg: BookConfig) -> dict[str, Any]:
    raw = cfg.generation.get("sourceLayoutRepair")
    settings = raw if isinstance(raw, dict) else {}
    mode = str(settings.get("mode", "auto")).lower()
    if mode not in {"auto", "off"}:
        mode = "auto"
    return {
        "mode": mode,
        "min_confidence": _float_setting(settings.get("minConfidence"), 0.85),
        "max_candidates": _int_setting(settings.get("maxCandidatesPerSource"), 20),
    }


def _float_setting(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _int_setting(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


async def structure_node(state: State, cfg: BookConfig) -> State:
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    results: list[CacheResult] = []
    summaries = []
    book_notes = cfg.book_notes
    for path in source_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        result = await run_with_cache(
            SourceSummaryAgent,
            {
                "path": str(path),
                "sha256": sha256_text(text),
                "language": cfg.language,
                "book_notes": book_notes,
            },
            model=cfg.model_for("source_summary"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        results.append(result)
        summaries.append(_json_model(result.result))

    structure = await run_with_cache(
        StructureAgent,
        {
            "summaries": summaries,
            "strategy": "pedagogical",
            "language": cfg.language,
            "book_notes": book_notes,
        },
        model=cfg.model_for("structure"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    results.append(structure)

    out_dir = ensure_dir(cfg.work_dir / "structure")
    proposed_path = write_text(
        out_dir / "proposed-structure.yaml", structure.result.proposed_structure_yaml
    )
    approved_path = out_dir / "approved-structure.yaml"
    approved_needs_seed = (
        not approved_path.exists() or not approved_path.read_text(encoding="utf-8").strip()
    )
    if approved_needs_seed:
        write_text(
            approved_path,
            _pending_approved_structure_text(structure.result.proposed_structure_yaml),
        )
    write_text(
        out_dir / "structure-review.md",
        "# Structure Review\n\n"
        "Review `proposed-structure.yaml`, edit `approved-structure.yaml`, then replace "
        f"`{PENDING_STRUCTURE_MARKER}` with `{APPROVED_STRUCTURE_MARKER}` before running split.\n\n"
        f"Source summaries: {len(summaries)}\n",
    )

    return {
        "proposed_structure": _rel(proposed_path, cfg.book_dir),
        "approved_structure": _rel(approved_path, cfg.book_dir),
        "cache_hit": _stage_cache_hit(results),
    }


async def split_node(state: State, cfg: BookConfig) -> State:
    approved_path = cfg.book_dir / state.get(
        "approved_structure", "work/structure/approved-structure.yaml"
    )
    approved_structure = approved_path.read_text(encoding="utf-8")
    _assert_structure_approved(approved_structure)
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    split = await run_with_cache(
        ChapterSplitAgent,
        {
            "source_paths": [str(path) for path in source_paths],
            "source_hashes": [
                sha256_text(path.read_text(encoding="utf-8", errors="ignore"))
                for path in source_paths
            ],
            "approved_structure": approved_structure,
            "book_notes": cfg.book_notes,
        },
        model=cfg.model_for("split"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )

    out_dir = ensure_dir(cfg.work_dir / "chapter_sources")
    chapter_sources: dict[str, str] = {}
    titles = split.result.chapter_titles or dict(_chapter_titles(approved_structure))
    # Authoritative reading order (approved-structure / YAML order, appendix last). Fall back to
    # the rendered chapters dict order for any legacy cached split result without chapter_order.
    chapter_order = list(split.result.chapter_order) or list(split.result.chapters.keys())
    _clear_chapter_source_dirs(out_dir, set(chapter_order))
    for ch_id in chapter_order:
        md = split.result.chapters[ch_id]
        title = titles.get(ch_id, ch_id)
        chapter_dir = ensure_dir(out_dir / ch_id)
        path = write_text(
            chapter_dir / "source.md",
            md if md.startswith("#") else f"# {title}\n\n{md.strip()}\n",
        )
        chapter_sources[ch_id] = _rel(path, cfg.book_dir)
    alignment_path = write_json(
        out_dir / "_alignment.json",
        {
            "alignment": split.result.alignment,
            "coverage": split.result.coverage,
            "chapter_titles": titles,
            "chapter_groups": split.result.chapter_groups,
            "chapter_order": chapter_order,
        },
    )
    report_path = write_text(
        cfg.work_dir / "logs" / "chapter-split-report.md", split.result.report_md
    )

    return {
        "chapter_sources": chapter_sources,
        "chapter_titles": titles,
        "chapter_order": chapter_order,
        "chapter_topics": _chapter_topics(approved_structure),
        "chapter_groups": split.result.chapter_groups,
        "chapter_alignment": _rel(alignment_path, cfg.book_dir),
        "chapter_split_report": _rel(report_path, cfg.book_dir),
        "cache_hit": split.cache_hit,
    }


def _clear_chapter_source_dirs(out_dir: Path, keep_ids: set[str]) -> None:
    """Remove stale chapter source directories before writing the fresh split.

    Any subdirectory whose name is not in ``keep_ids`` is removed. This intentionally does NOT
    rely on a ``chapter-N`` naming pattern, so free-form / CJK chapter slugs from a previous run
    that are no longer present are still cleaned up (a leftover dir would otherwise be picked up
    by a stale resume).
    """
    for child in out_dir.iterdir():
        if child.is_dir() and child.name not in keep_ids:
            shutil.rmtree(child)


async def build_skeleton_node(state: State, cfg: BookConfig) -> State:
    """Produce the read-only book-wide skeleton consumed by ``generate``.

    Sits between ``split`` and ``generate``: gathers each chapter's title,
    curated topics, and source markdown, then asks ``SkeletonAgent`` to
    produce a canonical glossary, alias map, one-line chapter briefs, and
    chapter order. The skeleton is written to ``work/skeleton.json`` and the
    relative path is stored in state under ``skeleton``.
    """
    if not state.get("chapter_sources"):
        msg = "build_skeleton requires chapter_sources; run split before build_skeleton"
        raise ValueError(msg)

    chapter_sources: dict[str, str] = state["chapter_sources"]
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})

    chapters_payload: list[dict[str, Any]] = []
    for ch_id in chapter_sources:
        rel_source = chapter_sources[ch_id]
        source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
        chapters_payload.append(
            {
                "chapter_id": ch_id,
                "title": _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id))),
                "topics": list(topics_by_chapter.get(ch_id, [])),
                "source_refs": _extract_source_refs(source_md),
                "source_md": source_md,
            }
        )

    payload: dict[str, Any] = {
        "chapters": chapters_payload,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
    }

    skeleton = await run_with_cache(
        SkeletonAgent,
        payload,
        model=cfg.model_for("skeleton"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    out_path = write_json(
        cfg.work_dir / "skeleton.json",
        _agent_result_payload(SkeletonAgent, cfg.model_for("skeleton"), skeleton.result),
    )
    return {
        "skeleton": _rel(out_path, cfg.book_dir),
        "cache_hit": skeleton.cache_hit,
    }


def _extract_source_refs(source_md: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"<!--\s*source_ref:\s*([^\s>]+)\s*-->", source_md):
        ref = match.group(1).strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _load_skeleton(state: State, cfg: BookConfig) -> dict[str, Any] | None:
    """Load the skeleton produced by ``build_skeleton_node`` if present.

    Returns the inner ``BookSkeleton`` payload (without the ``_agent`` wrapper),
    or ``None`` when the state has no ``skeleton`` key (e.g. old runs that
    pre-date M2 or partial reruns).
    """
    rel_path = state.get("skeleton")
    if not rel_path:
        return None
    payload = read_json(cfg.book_dir / rel_path, default={})
    return _agent_result(payload) or None


def _skeleton_payload(skeleton: dict[str, Any] | None, ch_id: str) -> dict[str, Any]:
    """Project the skeleton into the per-chapter section-generation payload.

    Each section generator (``SectionPlannerAgent`` / ``SectionAgent``) receives:

    - ``glossary``: full canonical concept list (every chapter sees the same
      table so terminology converges).
    - ``alias_map``: every variant → canonical, so the LLM rewrites raw
      mentions into canonical names.
    - ``chapter_owns``: concepts whose ``first_chapter_id`` equals ``ch_id``;
      this chapter owns the definition.
    - ``chapter_uses``: concepts owned by other chapters; only reference
      them, do not redefine.
    - ``prev_brief`` / ``next_brief``: neighbouring chapter one-liners so the
      author can write transitions without seeing the actual generated body.
    """
    if not skeleton:
        return {}
    glossary = skeleton.get("glossary", []) or []
    alias_map = skeleton.get("alias_map", {}) or {}
    chapter_briefs = skeleton.get("chapter_briefs", {}) or {}
    chapter_order: list[str] = list(skeleton.get("chapter_order", []) or [])

    owns: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    for entry in glossary:
        if not isinstance(entry, dict):
            continue
        first = str(entry.get("first_chapter_id") or "")
        bucket = owns if first == ch_id else uses
        bucket.append(
            {
                "canonical": entry.get("canonical"),
                "aliases": entry.get("aliases", []),
                "first_chapter_id": first,
            }
        )

    prev_brief = ""
    next_brief = ""
    if ch_id in chapter_order:
        position = chapter_order.index(ch_id)
        if position > 0:
            prev_brief = chapter_briefs.get(chapter_order[position - 1], "")
        if position + 1 < len(chapter_order):
            next_brief = chapter_briefs.get(chapter_order[position + 1], "")

    return {
        "glossary": glossary,
        "alias_map": alias_map,
        "chapter_owns": owns,
        "chapter_uses": uses,
        "prev_brief": prev_brief,
        "next_brief": next_brief,
    }


async def generate_node(state: State, cfg: BookConfig) -> State:
    if not state.get("chapter_sources"):
        msg = "generate requires chapter_sources; run split before generate"
        raise ValueError(msg)
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})
    skeleton_data = _load_skeleton(state, cfg)

    section_model = cfg.model_for("section")
    application_quiz_model = cfg.model_for("application_quiz")
    card_model = cfg.model_for("card")
    summary_model = cfg.model_for("summary")

    chapter_items = list(state.get("chapter_sources", {}).items())
    semaphore = asyncio.Semaphore(cfg.chapter_concurrency)

    async def run_chapter(ch_id: str, rel_source: str):
        async with semaphore:
            source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
            title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
            return await generate_chapter_sections(
                cfg=cfg,
                chapter_id=ch_id,
                title=title,
                source_md=source_md,
                source_path=rel_source,
                topics=list(topics_by_chapter.get(ch_id, [])),
                figures=_source_figures(source_md),
                skeleton_payload=_skeleton_payload(skeleton_data, ch_id),
            )

    # Chapters generate in parallel (chapter-level fan-out; sections within a chapter
    # also fan out, bounded by cfg.section_concurrency - see generate.sections),
    # bounded by ``cfg.chapter_concurrency``. ``asyncio.gather`` preserves input order.
    # ``return_exceptions=True`` so one chapter's failure does not discard the
    # in-progress work of its siblings: successful chapters are still written (and
    # cached), then we fail loudly listing the broken chapters so a resume reruns
    # only those.
    generated_list = await asyncio.gather(
        *(run_chapter(ch_id, rel_source) for ch_id, rel_source in chapter_items),
        return_exceptions=True,
    )

    chapter_results: dict[str, dict[str, str]] = {}
    chapter_cache_hits: list[bool] = []
    generation_issues: list[dict[str, Any]] = []
    generated_figures: dict[str, dict[str, str]] = {}
    failures: list[tuple[str, BaseException]] = []
    for (ch_id, _rel_source), generated in zip(chapter_items, generated_list, strict=True):
        if isinstance(generated, BaseException):
            failures.append((ch_id, generated))
            continue
        chapter_cache_hits.append(generated.cache_hit)
        generation_issues.extend(issue.model_dump(mode="json") for issue in generated.issues)
        if generated.generated_figures:
            generated_figures[ch_id] = dict(generated.generated_figures)
        paths = {
            "chapter": write_json(
                result_dir / f"{ch_id}.chapter.json",
                _agent_result_payload(SectionAgent, section_model, generated.chapter),
            ),
            "summary": write_json(
                result_dir / f"{ch_id}.summary.json",
                _agent_result_payload(SummaryAgent, summary_model, generated.summary),
            ),
            "quiz": write_json(
                result_dir / f"{ch_id}.quiz.json",
                _agent_result_payload(ApplicationQuizAgent, application_quiz_model, generated.quiz),
            ),
            "card": write_json(
                result_dir / f"{ch_id}.card.json",
                _agent_result_payload(CardAgent, card_model, generated.card),
            ),
        }
        chapter_results[ch_id] = {name: _rel(path, cfg.book_dir) for name, path in paths.items()}

    if failures:
        failed_ids = [ch_id for ch_id, _exc in failures]
        for ch_id, exc in failures:
            _LOG.error("generate failed for chapter %s: %s", ch_id, exc)
        msg = (
            f"generate failed for chapters: {failed_ids}; "
            f"{len(chapter_results)} chapter(s) completed and were written"
        )
        raise RuntimeError(msg) from failures[0][1]

    return {
        "agent_results": chapter_results,
        "generation_issues": generation_issues,
        "generated_figures": generated_figures,
        "cache_hit": bool(chapter_cache_hits) and all(chapter_cache_hits),
    }


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    agent_results = {
        str(ch_id): dict(paths) for ch_id, paths in state.get("agent_results", {}).items()
    }
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    cache_results: list[CacheResult] = []
    for ch_id, paths in state.get("agent_results", {}).items():
        if "concepts" in paths:
            extract = _agent_result(read_json(cfg.book_dir / paths["concepts"]))
            candidates.extend(extract.get("concepts", []))
            continue
        chapter = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        extract_result = await run_with_cache(
            ConceptExtractAgent,
            chapter,
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(extract_result)
        concepts_path = write_json(
            result_dir / f"{ch_id}.concepts.json",
            _agent_result_payload(
                ConceptExtractAgent, cfg.model_for("concept"), extract_result.result
            ),
        )
        agent_results.setdefault(str(ch_id), dict(paths))["concepts"] = _rel(
            concepts_path, cfg.book_dir
        )
        candidates.extend(item.model_dump(mode="json") for item in extract_result.result.concepts)

    skeleton = _load_skeleton(state, cfg)
    if skeleton is not None:
        reconciled_model = _merge_candidates_with_skeleton(skeleton, candidates)
    else:
        # Fallback to the legacy LLM-driven reconcile when no skeleton exists
        # (e.g. partial reruns that skipped build_skeleton).
        reconcile = await run_with_cache(
            ConceptReconcileAgent,
            {
                "candidates": candidates,
                "language": cfg.language,
                "book_notes": cfg.book_notes,
            },
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(reconcile)
        reconciled_model = reconcile.result

    out_dir = ensure_dir(cfg.work_dir / "concepts")
    reconciled = write_json(out_dir / "reconciled.json", _json_model(reconciled_model))
    write_json(
        cfg.work_dir / "agent_results" / "concepts.reconciled.json",
        _json_model(reconciled_model),
    )
    alias_map = write_json(out_dir / "alias_map.json", reconciled_model.alias_map)
    return {
        "reconciled_concepts": _rel(reconciled, cfg.book_dir),
        "alias_map": _rel(alias_map, cfg.book_dir),
        "agent_results": agent_results,
        "cache_hit": _stage_cache_hit(cache_results),
    }


def _merge_candidates_with_skeleton(
    skeleton: dict[str, Any], candidates: list[dict[str, Any]]
) -> ConceptReconcileResult:
    """Use the skeleton glossary as the pre-merged base and add any new candidates.

    Skeleton's glossary is already LLM-canonicalised at the ``build_skeleton``
    stage, so this step is purely deterministic: each candidate's normalised
    name is looked up in the skeleton's ``alias_map``; matches attach
    ``source_chapter_id`` to the existing canonical entry; non-matches are
    accumulated as new canonical concepts (rare — these are concepts a
    SectionAgent invented beyond the skeleton).
    """
    from bookwiki.agents.concept_reconcile import _concept_key

    by_key: dict[str, ConceptReconciledItem] = {}
    alias_to_key: dict[str, str] = {}

    for entry in skeleton.get("glossary", []) or []:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical") or "").strip()
        if not canonical:
            continue
        first_chapter = str(entry.get("first_chapter_id") or "").strip()
        aliases = [str(a) for a in entry.get("aliases", []) if str(a).strip()]
        key = _concept_key(canonical)
        item = ConceptReconciledItem(
            canonical=canonical,
            aliases=aliases,
            source_chapter_ids=[first_chapter] if first_chapter else [],
        )
        by_key[key] = item
        alias_to_key[key] = key
        for alias in aliases:
            alias_to_key[_concept_key(alias)] = key

    skeleton_alias_map = dict(skeleton.get("alias_map", {}) or {})
    for variant, canonical in skeleton_alias_map.items():
        key = _concept_key(canonical)
        if key in by_key:
            alias_to_key[_concept_key(variant)] = key

    for cand in candidates:
        canonical = str(cand.get("name") or "").strip()
        if not canonical:
            continue
        aliases = [str(a) for a in cand.get("aliases", []) if str(a).strip()]
        chapter_id = str(cand.get("source_chapter_id") or "").strip()
        if not chapter_id:
            msg = f"concept candidate {canonical!r} is missing required 'source_chapter_id'"
            raise ValueError(msg)
        names = [canonical, *aliases]
        matched_key = next(
            (alias_to_key[_concept_key(n)] for n in names if _concept_key(n) in alias_to_key),
            None,
        )
        key = matched_key or _concept_key(canonical)
        existing = by_key.get(key)
        if existing is None:
            existing = ConceptReconciledItem(
                canonical=canonical,
                aliases=[],
                source_chapter_ids=[chapter_id],
            )
            by_key[key] = existing
        elif chapter_id and chapter_id not in existing.source_chapter_ids:
            existing.source_chapter_ids.append(chapter_id)
        for name in names:
            normalized = _concept_key(name)
            alias_to_key[normalized] = key
            if name != existing.canonical and name not in existing.aliases:
                existing.aliases.append(name)

    alias_map: dict[str, str] = dict(skeleton_alias_map)
    for item in by_key.values():
        alias_map[item.canonical] = item.canonical
        alias_map[_concept_key(item.canonical)] = item.canonical
        for alias in item.aliases:
            alias_map[alias] = item.canonical
            alias_map[_concept_key(alias)] = item.canonical

    return ConceptReconcileResult(concepts=list(by_key.values()), alias_map=alias_map)


async def concept_pages_node(state: State, cfg: BookConfig) -> State:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    _clear_generated_files(out_dir, "*.json")
    outputs: dict[str, str] = {}
    cache_results: list[CacheResult] = []
    concept_generation_issues: list[dict[str, Any]] = []
    used_stems: set[str] = set()
    # Canonical names of every concept in the book, so each ConceptAgent picks
    # its `related` from a valid vocabulary (cuts down on unresolvable edges in
    # the homepage concept graph).
    glossary_names = [
        str(c.get("canonical")) for c in data.get("concepts", []) if c.get("canonical")
    ]
    for item in data.get("concepts", []):
        chapter_contexts = _concept_contexts(item, state, cfg)
        concept_input = {
            **item,
            "chapter_contexts": chapter_contexts,
            "glossary": glossary_names,
            "language": cfg.language,
            "book_notes": cfg.book_notes,
        }
        result = await run_with_cache(
            ConceptAgent,
            concept_input,
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(result)
        concept, inline_cache, inline_issue = await _validate_concept_artifact_inline(
            cfg=cfg,
            concept=result.result,
            chapter_contexts=chapter_contexts,
        )
        cache_results.extend(inline_cache)
        if inline_issue is not None:
            concept_generation_issues.append(inline_issue.model_dump(mode="json"))
        safe_name = _unique_file_stem(concept.name, used_stems, fallback_prefix="concept")
        path = write_json(out_dir / f"{safe_name}.json", _json_model(concept))
        outputs[concept.name] = _rel(path, cfg.book_dir)
    return {
        "concept_pages": outputs,
        "concept_generation_issues": concept_generation_issues,
        "cache_hit": _stage_cache_hit(cache_results),
    }


async def _validate_concept_artifact_inline(
    *,
    cfg: BookConfig,
    concept: ConceptResult,
    chapter_contexts: list[dict[str, Any]],
) -> tuple[ConceptResult, list[CacheResult], Issue | None]:
    cache_results: list[CacheResult] = []
    allowed_refs = _allowed_refs_from_concept_contexts(chapter_contexts)
    max_mdx_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    max_quality_rounds = int(cfg.generation.get("maxQualityRounds", 1) or 1)
    mdx_rounds = 0
    quality_rounds = 0
    best_concept = concept
    best_issues: list[ArtifactIssue] | None = None

    while True:
        issues = await validate_artifact(
            body_md=concept.body_md,
            kind="concept",
            allowed_refs=allowed_refs,
            cfg=cfg,
        )
        if not issues:
            return concept, cache_results, None
        if best_issues is None or len(issues) < len(best_issues):
            best_concept = concept
            best_issues = issues

        mdx_issues = [issue for issue in issues if issue.kind == "mdx"]
        quality_issues = [issue for issue in issues if issue.kind == "quality"]
        if mdx_issues:
            if mdx_rounds >= max_mdx_rounds:
                break
            mdx_rounds += 1
            repaired = await run_with_cache(
                ConceptMdxEditRepairAgent,
                {
                    **concept.model_dump(mode="json"),
                    "mdx_errors": [issue.message for issue in mdx_issues],
                    "language": cfg.language,
                    "book_notes": cfg.book_notes,
                    "allowed_source_refs": sorted(allowed_refs),
                },
                model=cfg.model_for("mdx_repair"),
                cache_dir=_cache_dir(cfg),
                force=True,
                runtime=cfg.llm_runtime,
            )
            cache_results.append(repaired)
            candidate = repaired.result
            if _body_too_short(candidate.body_md, concept.body_md):
                _LOG.warning(
                    "discarding concept MDX repair for %s: body shrank from %d to %d chars",
                    concept.name,
                    len(concept.body_md),
                    len(candidate.body_md),
                )
                continue
            concept = candidate
            continue
        if quality_issues:
            if quality_rounds >= max_quality_rounds:
                break
            quality_rounds += 1
            rewritten = await run_with_cache(
                ConceptContentRewriteAgent,
                {
                    **concept.model_dump(mode="json"),
                    "quality_findings": _quality_findings_from_artifact_issues(quality_issues),
                    "language": cfg.language,
                    "book_notes": cfg.book_notes,
                    "allowed_source_refs": sorted(allowed_refs),
                },
                model=cfg.model_for("quality_rewrite"),
                cache_dir=_cache_dir(cfg),
                force=True,
                runtime=cfg.llm_runtime,
            )
            cache_results.append(rewritten)
            candidate = rewritten.result
            if _body_too_short(candidate.body_md, concept.body_md):
                _LOG.warning(
                    "discarding concept quality rewrite for %s: body shrank from %d to %d chars",
                    concept.name,
                    len(concept.body_md),
                    len(candidate.body_md),
                )
                continue
            concept = candidate
            continue
        break

    final_concept = best_concept if best_issues is not None else concept
    final_issues = best_issues if best_issues is not None else []
    return (
        final_concept,
        cache_results,
        Issue(
            severity="warning",
            code="CONCEPT_VALIDATION_UNRESOLVED",
            message=(
                f"{final_concept.name} concept kept after inline validation rounds: "
                f"{'; '.join(issue.message for issue in final_issues)}"
            ),
            owner_task_id=str(final_concept.owner_task_id or f"concept:{final_concept.name}"),
        ),
    )


def _allowed_refs_from_concept_contexts(chapter_contexts: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for context in chapter_contexts:
        refs.update(SOURCE_REF_RE.findall(str(context.get("source_md") or "")))
    return refs


def _quality_findings_from_artifact_issues(
    issues: list[ArtifactIssue],
) -> list[dict[str, str]]:
    return [
        {"quote": issue.quote, "explanation": issue.explanation or "language_leak"}
        for issue in issues
        if issue.kind == "quality"
    ]


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
    # empty caption-only box (a phantom "missing image", e.g. next to a quiz) and would
    # also pad a trailing "## Figures" section with image-less entries. Drop those so
    # only figures that actually show the referenced image survive.
    return {
        figure_id: tag
        for figure_id, tag in index.items()
        if unescape(parse_book_figure_tag(tag).get("src", "")).strip()
    }


def _resolve_chapter_figures(body: str, index: dict[str, str]) -> tuple[str, str]:
    """Resolve inline ``<BookFigure/>`` references against the chapter figure index.

    Known references are rewritten to their canonical (src/caption-bearing) tag and
    marked used; unknown references are dropped so hallucinated ids never reach the
    page. Figures present in the source but never referenced are returned as a
    trailing ``## Figures`` section so no asset is silently lost.
    """
    used: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        figure_id = unescape(parse_book_figure_tag(match.group(0)).get("id", ""))
        canonical = index.get(figure_id)
        if canonical is None:
            return ""
        used.add(figure_id)
        return canonical

    resolved = BOOK_FIGURE_TAG_RE.sub(_replace, body)
    unused = [tag for figure_id, tag in index.items() if figure_id not in used]
    figures_md = "\n\n## Figures\n\n" + "\n\n".join(unused) if unused else ""
    return resolved, figures_md


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


def integrate_node(state: State, cfg: BookConfig) -> State:
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
        resolved_body, figures_md = _resolve_chapter_figures(
            rendered_body, _chapter_figure_index(state, cfg, ch_id)
        )
        chapter_path = chapters_dir / f"{doc_slug}.mdx"
        ensure_dir(chapter_path.parent)
        write_text(
            chapter_path,
            (
                _frontmatter(
                    {
                        "chapter_id": ch_id,
                        "title": display_title,
                        "type": "chapter",
                        "order_index": chapter_order_index,
                        "summary": summary["summary_md"],
                        "concepts": concept_names,
                    }
                )
                + resolved_body
                + figures_md
                + "\n\n"
                + f"## Sources\n\n{citation_md}\n\n"
                + f"## Anki Cards\n\n{card_mdx}\n"
            ),
        )
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
        write_text(
            concepts_dir / f"{safe_name}.mdx",
            _frontmatter({"title": concept["name"], "type": "concept"})
            + f"# {concept['name']}\n\n"
            + convert_html_style_attrs(
                normalize_source_cites(normalize_mdx_math(str(concept["body_md"])))
            )
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
    stitching = audit_stitching(content_dir, alias_map)
    if not stitching.ok:
        _LOG.warning(
            "stitching audit found issues book_id=%s term_drift=%d unresolved_xrefs=%d",
            cfg.book_id,
            len(stitching.term_drift),
            len(stitching.unresolved_xrefs),
        )
    return {"content_ready": True, "content_index": _rel(index_path, cfg.book_dir)}


def _require_mdx_validator(cfg: BookConfig) -> None:
    """Fail loudly if the MDX validator is unavailable, unless explicitly waived.

    When Node / the bundled validator's ``node_modules`` are missing, ``validate_mdx``
    silently returns ``[]`` ("no errors") - which would disable every inline AND macro
    MDX check at once and let broken MDX reach the rendered site. The ``check`` stage is
    the last gate, so it refuses to run blind. ``generation.allowMissingMdxValidator``
    is the escape hatch for environments that knowingly have no Node (degrades to a
    single loud error instead of aborting).
    """
    if mdx_validator_available():
        return
    if cfg.generation.get("allowMissingMdxValidator"):
        _LOG.error(
            "mdx validator unavailable but allowMissingMdxValidator=true; "
            "MDX compile checks are DISABLED for this run"
        )
        return
    msg = (
        "mdx validator unavailable: install Node and run `npm install` in "
        "tools/mdx-validate (or set generation.allowMissingMdxValidator=true to "
        "skip MDX checks). Refusing to run check blind."
    )
    raise RuntimeError(msg)


async def check_node(state: State, cfg: BookConfig) -> State:
    _require_mdx_validator(cfg)
    issues: list[Issue] = []
    for raw_issue in state.get("generation_issues", []):
        if isinstance(raw_issue, dict):
            issues.append(Issue.model_validate(raw_issue))
    for raw_issue in state.get("concept_generation_issues", []):
        if isinstance(raw_issue, dict):
            issues.append(Issue.model_validate(raw_issue))
    if not (cfg.content_dir / "index.mdx").exists():
        issues.append(
            Issue(
                severity="error",
                code="MISSING_CONTENT_INDEX",
                message="content/docs/index.mdx was not generated",
                owner_task_id="content:index",
            )
        )
    for path in (cfg.content_dir / "chapters").rglob("*.mdx"):
        text = path.read_text(encoding="utf-8")
        mdx_errors = validate_mdx(text)
        for error in mdx_errors:
            issues.append(
                Issue(
                    severity="error",
                    code="MDX_PARSE_ERROR",
                    message=f"{path.name} fails MDX compilation: {error}",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        if not text.startswith("---\n"):
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_FRONTMATTER",
                    message=f"{path.name} has no YAML frontmatter",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        if "<QuizBlock" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_QUIZ",
                    message=f"{path.name} has no QuizBlock",
                    owner_task_id=f"{path.stem}:quiz",
                )
            )
        elif not mdx_errors:
            issues.extend(_inline_quiz_answer_issues(text, path.stem))
        if "## Anki Cards" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_ANKI",
                    message=f"{path.name} has no Anki Cards section",
                    owner_task_id=f"{path.stem}:card",
                )
            )
        if "## Sources" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_SOURCES",
                    message=f"{path.name} has no Sources section",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        for phrase in _suspicious_phrases(text):
            issues.append(
                Issue(
                    severity="warning",
                    code="SUSPICIOUS_INSTRUCTION",
                    message=f"{path.name} contains suspicious instruction text: {phrase}",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
        for target in re.findall(r"\]\((?!https?://|mailto:|#)([^)]+)\)", text):
            if not _mdx_link_exists(path.parent, target):
                issues.append(
                    Issue(
                        severity="error",
                        code="BROKEN_LINK",
                        message=f"{path.name} links to missing target {target}",
                        owner_task_id=f"{path.stem}:chapter",
                    )
                )

    for path in sorted((cfg.content_dir / "concepts").glob("*.mdx")):
        text = path.read_text(encoding="utf-8")
        for error in validate_mdx(text):
            issues.append(
                Issue(
                    severity="error",
                    code="MDX_PARSE_ERROR",
                    message=f"{path.name} fails MDX compilation: {error}",
                    owner_task_id=f"concept-mdx:{path.stem}",
                )
            )

    allowed_refs = _allowed_source_refs(state, cfg)
    for ch_id, paths in state.get("agent_results", {}).items():
        for kind, rel_path in paths.items():
            payload = read_json(cfg.book_dir / rel_path)
            for ref_id in _iter_citation_refs(payload):
                if allowed_refs and ref_id not in allowed_refs:
                    issues.append(
                        Issue(
                            severity="error",
                            code="UNKNOWN_SOURCE_REF",
                            message=f"{rel_path} cites unknown source_ref {ref_id}",
                            owner_task_id=_artifact_owner_task_id(ch_id, kind, payload),
                        )
                    )
        quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
        for index, item in enumerate(quiz.get("items", []), start=1):
            choices = [str(choice) for choice in item.get("choices", [])]
            answer = str(item.get("answer", ""))
            if answer not in choices:
                issues.append(
                    Issue(
                        severity="error",
                        code="QUIZ_ANSWER_NOT_IN_CHOICES",
                        message=f"{ch_id} quiz item {index} answer is not in choices",
                        owner_task_id=f"{ch_id}:quiz",
                    )
                )
        card = _agent_result(read_json(cfg.book_dir / paths["card"]))
        for index, item in enumerate(card.get("items", []), start=1):
            if not str(item.get("front", "")).strip() or not str(item.get("back", "")).strip():
                issues.append(
                    Issue(
                        severity="error",
                        code="EMPTY_CARD_SIDE",
                        message=f"{ch_id} card item {index} has an empty side",
                        owner_task_id=f"{ch_id}:card",
                    )
                )
    for name, rel_path in state.get("concept_pages", {}).items():
        payload = read_json(cfg.book_dir / rel_path)
        owner = str(_agent_result(payload).get("owner_task_id") or f"concept:{name}")
        for ref_id in _iter_citation_refs(payload):
            if allowed_refs and ref_id not in allowed_refs:
                issues.append(
                    Issue(
                        severity="error",
                        code="UNKNOWN_SOURCE_REF",
                        message=f"{rel_path} cites unknown source_ref {ref_id}",
                        owner_task_id=owner,
                    )
                )
    status = "needs_repair" if issues else "passed"
    report = CheckReport(status=status, issues=issues)
    logs_dir = ensure_dir(cfg.work_dir / "logs")
    report_path = write_json(logs_dir / "check-report.json", report.model_dump(mode="json"))
    write_json(cfg.work_dir / "check-report.json", report.model_dump(mode="json"))
    write_text(logs_dir / "check-report.md", _render_check_report_md(report))
    return {
        "check_report": _rel(report_path, cfg.book_dir),
        "repair_targets": report.repair_targets,
    }


async def repair_node(state: State, cfg: BookConfig) -> State:
    targets = state.get("repair_targets", [])
    if not targets:
        return {"repair_targets": []}
    rounds = dict(state.get("_repair_rounds", {}))
    out_dir = ensure_dir(cfg.work_dir / "repairs")
    outputs = []
    repair_actions: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    report = read_json(cfg.book_dir / state.get("check_report", "work/logs/check-report.json"))
    for target in targets:
        target_issues = [
            issue for issue in report.get("issues", []) if issue.get("owner_task_id") == target
        ]
        codes = {str(issue.get("code")) for issue in target_issues}
        max_rounds = _repair_round_limit(codes, cfg)
        if int(rounds.get(target, 0)) >= max_rounds:
            # Exhausted: record it loudly instead of silently dropping the target, so
            # broken-but-unrepaired content reaching index is visible to the operator
            # (mirrors the inline loops' *_VALIDATION_UNRESOLVED warnings).
            exhausted.append(
                {
                    "owner_task_id": target,
                    "codes": sorted(codes),
                    "rounds": int(rounds.get(target, 0)),
                }
            )
            _LOG.warning(
                "repair exhausted target=%s codes=%s rounds=%d (kept unrepaired)",
                target,
                sorted(codes),
                int(rounds.get(target, 0)),
            )
            continue
        rounds[target] = int(rounds.get(target, 0)) + 1
        if "MDX_PARSE_ERROR" in codes:
            if target.startswith("concept-mdx:"):
                repaired = await _repair_concept_mdx(target, target_issues, state, cfg)
            else:
                repaired = await _repair_chapter_mdx(target, target_issues, state, cfg)
            if repaired is not None:
                path = write_json(
                    out_dir / f"{target.replace(':', '-')}.json", _json_model(repaired)
                )
                outputs.append(_rel(path, cfg.book_dir))
        else:
            result = await run_with_cache(
                ReviewAgent,
                {
                    "owner_task_id": target,
                    "issues": target_issues,
                    "previous_output": _owner_output_payload(target, state, cfg),
                },
                model=cfg.model_for("review"),
                cache_dir=_cache_dir(cfg),
                force=True,
                runtime=cfg.llm_runtime,
            )
            path = write_json(
                out_dir / f"{target.replace(':', '-')}.json", _json_model(result.result)
            )
            outputs.append(_rel(path, cfg.book_dir))
        action = _apply_repair(target, target_issues, state, cfg)
        if action is not None:
            repair_actions.append(action)
            _LOG.warning("repair applied destructive fix (content removed): %s", action)
    if repair_actions:
        write_json(
            ensure_dir(cfg.work_dir / "logs") / "repair-actions.json",
            {"actions": repair_actions},
        )
    if exhausted:
        write_json(
            ensure_dir(cfg.work_dir / "logs") / "repair-exhausted.json",
            {"exhausted": exhausted},
        )
    return {
        "repairs": outputs,
        "repair_targets": [],
        "_repair_rounds": rounds,
        "repair_exhausted": exhausted,
    }


def _repair_round_limit(codes: set[str], cfg: BookConfig) -> int:
    del codes
    return int(cfg.generation.get("maxRepairRounds", 1) or 1)


async def _repair_chapter_mdx(
    target: str, target_issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> Any | None:
    """Rewrite a chapter's body to fix MDX compilation errors via ``ChapterMdxEditRepairAgent``.

    Reads the chapter artifact, feeds its ``body_md`` plus the ``MDX_PARSE_ERROR``
    diagnostics to the LLM, and writes the repaired ``ChapterResult`` back so the next
    ``integrate`` re-renders it and ``check`` re-compiles. Returns the repaired result
    (or ``None`` if the chapter artifact is missing).
    """
    ch_id = target.partition(":")[0]
    chapter_rel = state.get("agent_results", {}).get(ch_id, {}).get("chapter")
    if not chapter_rel:
        return None
    chapter_path = cfg.book_dir / chapter_rel
    chapter = _agent_result(read_json(chapter_path))
    mdx_errors = [
        str(issue.get("message"))
        for issue in target_issues
        if str(issue.get("code")) == "MDX_PARSE_ERROR"
    ]
    repair_input = {
        **chapter,
        "mdx_errors": mdx_errors,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
        "allowed_source_refs": sorted(_allowed_source_refs(state, cfg)),
    }
    result = await run_with_cache(
        ChapterMdxEditRepairAgent,
        repair_input,
        model=cfg.model_for("mdx_repair"),
        cache_dir=_cache_dir(cfg),
        force=True,
        runtime=cfg.llm_runtime,
    )
    write_json(
        chapter_path,
        _agent_result_payload(
            ChapterMdxEditRepairAgent, cfg.model_for("mdx_repair"), result.result
        ),
    )
    return result.result


async def _repair_concept_mdx(
    target: str, target_issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> Any | None:
    """Rewrite a concept page body to fix MDX compilation errors via ``ConceptMdxEditRepairAgent``.

    The ``concept-mdx:<stem>`` target maps back to the concept artifact whose rendered
    filename is ``<stem>.mdx``. Feeds the concept ``body_md`` plus the ``MDX_PARSE_ERROR``
    diagnostics to the LLM and writes the repaired ``ConceptResult`` back (same unwrapped
    JSON shape ``concept_pages_node`` writes) so the next ``integrate`` re-renders it and
    ``check`` re-compiles. Returns the repaired result (or ``None`` if no artifact matches).
    """
    stem = target.partition(":")[2]
    concept_rel = next(
        (rel for rel in state.get("concept_pages", {}).values() if Path(rel).stem == stem),
        None,
    )
    if not concept_rel:
        return None
    concept_path = cfg.book_dir / concept_rel
    concept = read_json(concept_path)
    mdx_errors = [
        str(issue.get("message"))
        for issue in target_issues
        if str(issue.get("code")) == "MDX_PARSE_ERROR"
    ]
    repair_input = {
        **concept,
        "mdx_errors": mdx_errors,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
        "allowed_source_refs": sorted(_allowed_source_refs(state, cfg)),
    }
    result = await run_with_cache(
        ConceptMdxEditRepairAgent,
        repair_input,
        model=cfg.model_for("mdx_repair"),
        cache_dir=_cache_dir(cfg),
        force=True,
        runtime=cfg.llm_runtime,
    )
    write_json(concept_path, _json_model(result.result))
    return result.result


def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    build_sqlite_index(cfg.content_dir, db_path)
    return {"sqlite": _rel(db_path, cfg.book_dir)}


NODE_FUNCTIONS = {
    "convert": convert_node,
    "caption": caption_node,
    "structure": structure_node,
    "split": split_node,
    "build_skeleton": build_skeleton_node,
    "generate": generate_node,
    "reconcile_concepts": reconcile_node,
    "concept_pages": concept_pages_node,
    "integrate": integrate_node,
    "check": check_node,
    "repair": repair_node,
    "index": index_node,
}
