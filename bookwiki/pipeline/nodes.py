from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from html import escape, unescape
from pathlib import Path
from typing import Any

import yaml

from bookwiki.agents import (
    ApplicationQuizAgent,
    CardAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptContentRewriteAgent,
    ConceptExtractAgent,
    ConceptMdxEditRepairAgent,
    ConceptReconcileAgent,
    ExamAgent,
    ExamExplainAgent,
    MdxEditRepairAgent,
    ReviewAgent,
    SectionAgent,
    SkeletonExtractAgent,
    SkeletonFoldAgent,
    SourceLayoutRepairAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
    VisionCaptionAgent,
)
from bookwiki.agents._helpers import SOURCE_REF_RE
from bookwiki.checkers.mdx_validator import (
    mdx_validator_available,
    validate_mdx_many,
)
from bookwiki.checkers.quiz_extractor import QuizExtractError, extract_inline_quizzes
from bookwiki.chunking import chunk_by_heading
from bookwiki.concepts import brief_for as _brief_for
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.convert.common import (
    BOOK_FIGURE_TAG_RE,
    parse_book_figure_tag,
    source_id_from_stem,
)
from bookwiki.convert.mineru_client import convert_document_to_source
from bookwiki.convert.source_normalizer import (
    DecorativeImageThresholds,
    NormalizedSource,
    SourceBlock,
    _render_figure,
    normalize_structured_source,
)
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.generate.exam_pool import build_exam_pools
from bookwiki.generate.sections import _body_too_short, generate_chapter_sections
from bookwiki.generate.validate_artifact import ArtifactIssue, validate_artifact
from bookwiki.indexer.sqlite_builder import build_sqlite_index
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
from bookwiki.pipeline.structure_scan import audit_coverage, scan_source_refs
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.concept import ConceptReconciledItem, ConceptReconcileResult, ConceptResult
from bookwiki.schemas.quiz import ExamResult
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.schemas.source import DetectedExamQuestion, SourceSummaryResult
from bookwiki.skeleton.fold import Registry
from bookwiki.split.chapter_splitter import compute_slug_remap, parse_approved_structure
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_file, sha256_text
from bookwiki.utils.logging import get_logger

State = dict[str, Any]

_LOG = get_logger(__name__)

APPROVED_STRUCTURE_MARKER = "# bookwiki: approved-structure"
PENDING_STRUCTURE_MARKER = "# bookwiki: pending-structure-review"
CONVERT_ARTIFACT_VERSION = 2

_fanout_semaphores: dict[tuple[int, str], asyncio.Semaphore] = {}


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

    def _replace(match: re.Match[str]) -> str:
        id_match = re.search(r'id="([^"]*)"', match.group(0))
        entry = items_by_slot.get(id_match.group(1) if id_match else "")
        if entry is None:
            return ""
        item, index, kind = entry
        return _worked_problem_mdx(item, index) if kind == "worked" else _quiz_item_mdx(item, index)

    resolved = _QUIZ_ITEM_SLOT_RE.sub(_replace, body_md)
    return _EMPTY_QUIZ_BLOCK_RE.sub("", resolved)


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


# A code fence whose body holds an MDX component (e.g. ```quiz around <QuizBlock>): valid
# Markdown, so it passes MDX compilation, and the wrapped ``<QuizBlock`` keeps the page from
# tripping MISSING_QUIZ — yet the site's syntax highlighter throws on the unknown language at
# render time. We catch it deterministically and unwrap it. ``mermaid`` is the one legitimate
# component-free fence and is exempt.
_COMPONENT_FENCE_RE = re.compile(
    r"^```[ \t]*([A-Za-z0-9_-]+)?[^\n]*\n(.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
# Require the component tag to start its own line: a real wrapped component sits on its own line,
# whereas a string like ``print("<QuizBlock>")`` inside a legit code sample must not trip this.
_MDX_COMPONENT_RE = re.compile(
    r"^[ \t]*<(?:Quiz[A-Za-z]*|BookFigure|PreviewLink)\b", re.MULTILINE
)
_ALLOWED_FENCE_LANGS = {"mermaid"}
# Codes whose repair edits the rendered ``.mdx`` in place (then re-``check``), rather than
# re-running a source agent via ``integrate``.
_MDX_ROUTE_CODES = {"MDX_PARSE_ERROR", "ILLEGAL_CODE_FENCE"}


def _illegal_component_fence_issues(text: str, owner_task_id: str) -> list[Issue]:
    """Flag code fences that wrap MDX components (e.g. ```quiz around ``<QuizBlock>``)."""
    issues: list[Issue] = []
    for match in _COMPONENT_FENCE_RE.finditer(text):
        lang = (match.group(1) or "").lower()
        if lang in _ALLOWED_FENCE_LANGS:
            continue
        if _MDX_COMPONENT_RE.search(match.group(2)):
            issues.append(
                Issue(
                    severity="error",
                    code="ILLEGAL_CODE_FENCE",
                    message=(
                        f"code fence ```{lang or '(no lang)'} wraps an MDX component; "
                        "remove the fence so the component renders"
                    ),
                    owner_task_id=owner_task_id,
                )
            )
    return issues


def _strip_illegal_component_fences(text: str) -> str:
    """Remove component-wrapping code fences, keeping the component body (deterministic repair)."""

    def _unwrap(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").lower()
        if lang not in _ALLOWED_FENCE_LANGS and _MDX_COMPONENT_RE.search(match.group(2)):
            return match.group(2)
        return match.group(0)

    return _COMPONENT_FENCE_RE.sub(_unwrap, text)


def _normalize_rendered_mdx(content_dir: Path) -> int:
    """Normalize math delimiters across every rendered ``.mdx`` under ``content_dir``.

    integrate normalizes chapter/concept bodies as it renders them, but the homepage ``index.mdx``
    and any other write path are not individually covered. One final sweep here guarantees the
    single-source-of-truth ``content`` is fully normalized before validation/build — this replaces
    the second normalize pass that used to live in ``materialize_site``. Returns files changed.
    """
    changed = 0
    for path in content_dir.rglob("*.mdx"):
        text = path.read_text(encoding="utf-8")
        normalized = normalize_mdx_math(text)
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


def _suspicious_phrases(markdown: str) -> list[str]:
    phrases = ["ignore previous instructions", "system prompt", "developer message"]
    lower = markdown.lower()
    return [phrase for phrase in phrases if phrase in lower]


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

    _LOG.info(
        "convert: input_files=%d dir=%s",
        len(input_files),
        cfg.input_dir,
    )
    out_dir = ensure_dir(cfg.work_dir / "sources_md")
    manifest_dir = ensure_dir(cfg.work_dir / "source_refs")
    outputs: list[str] = []
    manifests: list[str] = []
    reused = 0
    converted = 0
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
            reused += 1
            continue
        suffix = path.suffix.lower()
        if suffix in {
            ".pdf",
            ".pptx",
            ".ppt",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".odt",
            ".odp",
            ".ods",
        }:
            _LOG.info("convert: mineru source=%s suffix=%s", path.name, suffix)
            parsed = convert_document_to_source(path, source_id=source_id)
            _materialize_mineru_assets(parsed, source_id, cfg)
            normalized = await _normalize_with_layout_repair(parsed, source_id, cfg)
            body = normalized.markdown
            manifest = normalized.manifest
        elif suffix in {".txt", ".md"}:
            _LOG.info("convert: text source=%s suffix=%s", path.name, suffix)
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
        converted += 1
        _LOG.info(
            "convert: wrote source_id=%s markdown=%d bytes",
            source_id,
            len(body),
        )

    _LOG.info(
        "convert: done converted=%d reused=%d outputs=%d manifests=%d",
        converted,
        reused,
        len(outputs),
        len(manifests),
    )
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
    _LOG.info(
        "caption: mode=%s sources=%d manifests=%d max_images=%d max_concurrent=%d",
        settings["mode"],
        len(source_mds),
        len(manifests),
        settings["max_images"],
        settings["max_concurrent"],
    )

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
            _LOG.info("caption: skip source_id=%s (mode=off)", source_id)
            continue

        normalized = NormalizedSource(markdown=md_text, manifest=manifest)
        candidates = _image_caption_candidates(normalized)[: settings["max_images"]]
        jobs = [_vision_caption_job(candidate, md_text) for candidate in candidates]
        _LOG.info(
            "caption: source_id=%s candidates=%d (after max cap=%d)",
            source_id,
            len(jobs),
            settings["max_images"],
        )
        outcomes = await _run_vision_caption_jobs(
            jobs,
            cfg,
            max_concurrent=settings["max_concurrent"],
        )
        source_hits = 0
        source_misses = 0
        source_failures = 0
        for job, outcome in zip(jobs, outcomes, strict=False):
            candidate = job["candidate"]
            if isinstance(outcome, Exception):
                warning = f"vision caption failed for {candidate['block_id']}: {outcome}"
                warnings.append(warning)
                caption_failures.append(warning)
                source_failures += 1
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
            if result.cache_hit:
                source_hits += 1
            else:
                source_misses += 1
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
        _LOG.info(
            "caption: source_id=%s done captions=%d hits=%d misses=%d failures=%d",
            source_id,
            source_hits + source_misses,
            source_hits,
            source_misses,
            source_failures,
        )
        # Deliberately do NOT write md_text back to sources_md. The convert artifact
        # (work/sources_md/*.md) must stay byte-identical to convert output so the convert
        # sha-idempotency gate (_matching_convert_artifact) keeps matching and MinerU output is
        # reused on rerun. Captions live only in the manifest (work/source_refs/*.json) and are
        # injected into the per-chapter sources at split time via _inject_book_figure_captions.

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
    decorative = _decorative_image_thresholds(cfg)
    normalized = normalize_structured_source(
        raw_md=str(parsed.get("markdown") or ""),
        source_id=source_id,
        content_list_v2=parsed.get("content_list_v2"),
        content_list=parsed.get("content_list"),
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
        asset_root=cfg.book_dir,
        decorative=decorative,
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
        asset_root=cfg.book_dir,
        decorative=decorative,
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
    if section_window is not None:
        candidate_input["section_context"] = section_window["text"]
    return {
        "candidate": candidate,
        "input": candidate_input,
        "source_ref": str(candidate.get("source_ref") or ""),
    }


async def _run_vision_caption_jobs(
    jobs: list[dict[str, Any]],
    cfg: BookConfig,
    *,
    max_concurrent: int,
) -> list[CacheResult | Exception]:
    outcomes: list[CacheResult | Exception | None] = [None] * len(jobs)
    indexed_jobs = [{**job, "index": index} for index, job in enumerate(jobs)]
    groups = _caption_same_page_groups(indexed_jobs)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_group_jobs(group_jobs: list[dict[str, Any]]) -> list[CacheResult | Exception]:
        try:
            async with semaphore:
                result = await _run_vision_caption_group(group_jobs, cfg)
            return _caption_group_outcomes(group_jobs, result)
        except Exception as exc:  # noqa: BLE001 - captioning is best-effort enrichment
            return [exc for _job in group_jobs]

    async def run_group(group: dict[str, Any]) -> None:
        group_jobs = sorted(group["jobs"], key=lambda item: int(item["index"]))
        group_outcomes = await run_group_jobs(group_jobs)
        for job, outcome in zip(group_jobs, group_outcomes, strict=False):
            outcomes[int(job["index"])] = outcome

    await asyncio.gather(*(run_group(group) for group in groups))
    return [
        item if item is not None else RuntimeError("caption job did not run") for item in outcomes
    ]


def _caption_same_page_groups(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    first_index: dict[str, int] = {}
    for job in sorted(jobs, key=lambda item: int(item["index"])):
        source_ref = str(job.get("source_ref") or "")
        buckets.setdefault(source_ref, []).append(job)
        first_index.setdefault(source_ref, int(job["index"]))
    return [
        {
            "source_ref": source_ref,
            "jobs": group_jobs,
        }
        for source_ref, group_jobs in sorted(buckets.items(), key=lambda item: first_index[item[0]])
    ]


async def _run_vision_caption_group(jobs: list[dict[str, Any]], cfg: BookConfig) -> CacheResult:
    agent_input = _vision_caption_group_agent_input(jobs, cfg)
    return await run_with_cache(
        VisionCaptionAgent,
        agent_input,
        model=cfg.model_for("vision"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )


def _vision_caption_group_agent_input(
    jobs: list[dict[str, Any]], cfg: BookConfig
) -> dict[str, Any]:
    images = [_vision_caption_agent_input(job["input"], cfg) for job in jobs]
    source_ref = str(jobs[0].get("source_ref") or "") if jobs else ""
    return {"source_ref": source_ref, "images": images}


def _caption_group_outcomes(
    jobs: list[dict[str, Any]], batch_result: CacheResult
) -> list[CacheResult]:
    captions = getattr(batch_result.result, "captions", [])
    by_block_id = {str(item.block_id): item for item in captions}
    outcomes: list[CacheResult] = []
    for job in jobs:
        candidate = job["candidate"]
        block_id = str(candidate["block_id"])
        item = by_block_id.get(block_id)
        if item is None:
            msg = f"batch caption missing result for {block_id}"
            raise RuntimeError(msg)
        outcomes.append(
            CacheResult(
                result=item,
                cache_hit=batch_result.cache_hit,
                key=batch_result.key,
                path=batch_result.path,
            )
        )
    return outcomes


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


def _caption_blocks_by_id(cfg: BookConfig) -> dict[str, dict[str, Any]]:
    """Collect every manifest block keyed by ``block_id`` across all source manifests on disk.

    Captions are written into ``work/source_refs/*.json`` by the ``caption`` stage (which no
    longer mutates ``sources_md``). Reading the manifests straight from disk keeps split
    independent of pipeline-state threading, so ``--from split`` works even when
    ``source_ref_manifests`` was not re-seeded into state.
    """
    blocks: dict[str, dict[str, Any]] = {}
    refs_dir = cfg.work_dir / "source_refs"
    if not refs_dir.exists():
        return blocks
    for manifest_path in sorted(refs_dir.glob("*.json")):
        manifest = read_json(manifest_path, default={})
        if not isinstance(manifest, dict):
            continue
        for page in manifest.get("pages", []):
            if not isinstance(page, dict):
                continue
            for block in page.get("blocks", []):
                if isinstance(block, dict) and block.get("block_id"):
                    blocks[str(block["block_id"])] = block
    return blocks


def _inject_book_figure_captions(markdown: str, blocks_by_id: dict[str, dict[str, Any]]) -> str:
    """Re-render each ``<BookFigure id=.../>`` whose manifest block carries a caption.

    This lands vision captions inline in the per-chapter source while leaving ``sources_md``
    byte-identical to convert output. Only the first occurrence of each ``id`` is rewritten,
    mirroring the previous ``caption`` stage behaviour (``_replace_book_figure`` uses
    ``count=1``).
    """
    if not blocks_by_id:
        return markdown
    seen: set[str] = set()
    for tag in BOOK_FIGURE_TAG_RE.findall(markdown):
        block_id = unescape(parse_book_figure_tag(tag).get("id", ""))
        if not block_id or block_id in seen:
            continue
        seen.add(block_id)
        block = blocks_by_id.get(block_id)
        if block is None or not str(block.get("caption") or "").strip():
            continue
        markdown, _ = _replace_book_figure(markdown, block)
    return markdown


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


def _decorative_image_thresholds(cfg: BookConfig) -> DecorativeImageThresholds | None:
    """Build the decorative-image size floors from config, or ``None`` to disable.

    Set ``generation.decorativeImageFilter.mode = "off"`` to keep every extracted image
    block. The size keys override individual defaults of ``DecorativeImageThresholds``.
    """
    raw = cfg.generation.get("decorativeImageFilter")
    settings = raw if isinstance(raw, dict) else {}
    if str(settings.get("mode", "auto")).lower() == "off":
        return None
    defaults = DecorativeImageThresholds()
    return DecorativeImageThresholds(
        min_pixel_side=_int_setting(settings.get("minPixelSide"), defaults.min_pixel_side),
        min_pixel_area=_int_setting(settings.get("minPixelArea"), defaults.min_pixel_area),
        min_bbox_side=_float_setting(settings.get("minBboxSide"), defaults.min_bbox_side),
        min_bbox_area=_float_setting(settings.get("minBboxArea"), defaults.min_bbox_area),
    )


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


def _merge_source_chunk_summaries(
    source_id: str, chunks: list[SourceSummaryResult]
) -> SourceSummaryResult:
    if not chunks:
        return SourceSummaryResult(
            source_id=source_id,
            summary_md=f"Summary for {source_id}: no chunks produced.",
            source_refs=[],
        )

    source_refs: list[str] = []
    headings: list[str] = []
    key_terms: list[str] = []
    summary_parts: list[str] = []
    detected_chapters = []
    concept_candidates = []
    exam_questions = []
    is_exam = False
    detected_chapter_id: str | None = None
    detected_title: str | None = None

    for chunk in chunks:
        _append_unique_strings(source_refs, chunk.source_refs)
        _append_unique_strings(headings, chunk.headings)
        _append_unique_strings(key_terms, chunk.key_terms)
        if chunk.summary_md:
            summary_parts.append(chunk.summary_md)
        if chunk.detected_chapter_id and detected_chapter_id is None:
            detected_chapter_id = chunk.detected_chapter_id
        if chunk.detected_title and detected_title is None:
            detected_title = chunk.detected_title
        detected_chapters.extend(chunk.detected_chapters)
        concept_candidates.extend(chunk.concept_candidates)
        is_exam = is_exam or chunk.is_exam
        exam_questions.extend(chunk.exam_questions)

    if len(detected_chapters) == 1:
        detected_chapters = [
            detected_chapters[0].model_copy(update={"source_refs": list(source_refs)})
        ]

    return SourceSummaryResult(
        source_id=source_id,
        summary_md="\n\n".join(summary_parts),
        source_refs=source_refs,
        detected_chapter_id=detected_chapter_id,
        detected_title=detected_title,
        headings=headings,
        key_terms=key_terms,
        detected_chapters=detected_chapters,
        concept_candidates=concept_candidates,
        is_exam=is_exam,
        exam_questions=exam_questions,
    )


def _append_unique_strings(target: list[str], values: list[str]) -> None:
    seen = set(target)
    for value in values:
        if value in seen:
            continue
        target.append(value)
        seen.add(value)


def _concept_candidates_by_ref(
    summaries: list[SourceSummaryResult],
) -> dict[str, list[dict[str, Any]]]:
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        for candidate in summary.concept_candidates:
            payload = candidate.model_dump(mode="json")
            refs = candidate.source_refs or summary.source_refs
            for ref in refs:
                bucket = by_ref.setdefault(ref, [])
                if payload not in bucket:
                    bucket.append(payload)
    return by_ref


async def structure_node(state: State, cfg: BookConfig) -> State:
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    _LOG.info("structure: source_files=%d", len(source_paths))
    results: list[CacheResult] = []
    summaries = []
    merged_summaries: list[SourceSummaryResult] = []
    book_notes = cfg.book_notes
    model = cfg.model_for("source_summary")
    all_refs: set[str] = set()
    covered_refs: set[str] = set()
    for path in source_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        source_id = path.stem
        all_refs |= scan_source_refs(text)
        # Chunk the source so no single summary call can be silently truncated by
        # compact_input (the whole-book-in-one-call failure for >1M-token books). Each
        # chunk is bounded below the model's per-field budget by ``chunk_by_heading``.
        chunks = chunk_by_heading(text, model=model, stage="structure")
        spans = [(c.text, list(c.heading_path), list(c.source_refs)) for c in chunks] or [
            (text, [], sorted(all_refs))
        ]
        chunk_summaries: list[SourceSummaryResult] = []
        for span_text, heading_path, span_refs in spans:
            covered_refs.update(span_refs)
            result = await run_with_cache(
                SourceSummaryAgent,
                {
                    "span_text": span_text,
                    "source_id": source_id,
                    "path": str(path),
                    "heading_path": heading_path,
                    "sha256": sha256_text(span_text),
                    "language": cfg.language,
                    "book_notes": book_notes,
                },
                model=model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            )
            results.append(result)
            chunk_summaries.append(result.result)
        merged = _merge_source_chunk_summaries(source_id, chunk_summaries)
        merged_summaries.append(merged)
        summaries.append(_json_model(merged))

    # Coverage audit: every ``<!-- source_ref -->`` in the sources must land in some chunk.
    # A miss means a chunking gap / truncation silently dropped part of the book — fail
    # loudly instead of proposing a structure that omits chapters.
    missing = audit_coverage(all_refs, covered_refs)
    if missing:
        msg = (
            "structure stage coverage audit failed: these source_refs were dropped "
            f"between chunks (chunking gap or truncation): {missing[:20]}"
        )
        raise ValueError(msg)

    _LOG.info(
        "structure: summaries done count=%d cache_hits=%d",
        len(summaries),
        sum(1 for r in results if r.cache_hit),
    )

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
    write_json(out_dir / "concept-candidates.json", _concept_candidates_by_ref(merged_summaries))
    write_json(out_dir / "exam-pool.json", _collect_exam_questions(merged_summaries))
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
    cache_hits = sum(1 for r in results if r.cache_hit)
    _LOG.info(
        "structure: wrote proposed=%s approved_seed=%s cache_hits=%d/%d total=%d",
        _rel(proposed_path, cfg.book_dir),
        approved_needs_seed,
        cache_hits,
        len(results),
        len(results),
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
    _LOG.info(
        "split: approved_structure=%s cache_hit=%s",
        _rel(approved_path, cfg.book_dir),
        bool(approved_structure),
    )
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
    parse_titles = split.result.chapter_titles or dict(_chapter_titles(approved_structure))
    parse_order = list(split.result.chapter_order) or list(split.result.chapters.keys())
    registry_path = cfg.work_dir / "chapter_slugs.json"
    remap, registry = compute_slug_remap(
        parse_order,
        split.result.chapter_groups,
        parse_titles,
        split.result.chapter_source_refs,
        _load_slug_registry(registry_path),
    )
    write_json(registry_path, registry)

    def _rid(value: str) -> str:
        return remap.get(value, value)

    chapter_order = [_rid(ch_id) for ch_id in parse_order]
    titles = {_rid(k): v for k, v in parse_titles.items()}
    chapters_md = {_rid(k): v for k, v in split.result.chapters.items()}
    chapter_groups = {
        _rid(str(gid)): {
            **(info if isinstance(info, dict) else {}),
            "leaf_ids": [_rid(str(lid)) for lid in (info or {}).get("leaf_ids", []) or []],
        }
        for gid, info in split.result.chapter_groups.items()
    }
    alignment = [
        {**item, "chapter_id": _rid(str(item.get("chapter_id") or ""))}
        for item in split.result.alignment
    ]
    chapter_topics = {_rid(k): v for k, v in _chapter_topics(approved_structure).items()}

    _clear_chapter_source_dirs(out_dir, set(chapter_order))
    caption_blocks = _caption_blocks_by_id(cfg)
    _LOG.info(
        "split: chapters=%d chunked=%d groups=%d slug_remap=%d caption_blocks=%d",
        len(chapter_order),
        len(chapters_md),
        len(chapter_groups),
        len(remap),
        len(caption_blocks),
    )
    for ch_id in chapter_order:
        md = _inject_book_figure_captions(chapters_md[ch_id], caption_blocks)
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
            "alignment": alignment,
            "coverage": split.result.coverage,
            "chapter_titles": titles,
            "chapter_groups": chapter_groups,
            "chapter_order": chapter_order,
        },
    )
    report_path = write_text(
        cfg.work_dir / "logs" / "chapter-split-report.md", split.result.report_md
    )
    _LOG.info(
        "split: done chapter_sources=%d cache_hit=%s report=%s",
        len(chapter_sources),
        split.cache_hit,
        _rel(report_path, cfg.book_dir),
    )

    return {
        "chapter_sources": chapter_sources,
        "chapter_titles": titles,
        "chapter_order": chapter_order,
        "chapter_topics": chapter_topics,
        "chapter_groups": chapter_groups,
        "chapter_alignment": _rel(alignment_path, cfg.book_dir),
        "chapter_split_report": _rel(report_path, cfg.book_dir),
        "cache_hit": split.cache_hit,
    }


def _load_slug_registry(path: Path) -> dict[str, str]:
    """Load the persisted ``fingerprint -> slug`` chapter slug registry (empty when absent)."""
    if not path.exists():
        return {}
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


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

    Streams the build instead of shipping the whole book to one LLM call (which would
    overflow a >1M-token book's context window). Pass 1 extracts concept candidates from
    each chapter's source in parallel — the source is chunked first so no single call
    exceeds the model budget. Pass 2 folds those candidates chapter-by-chapter in order,
    letting the model merge cross-language synonyms against the compact running registry
    it sees in context. The resulting ``BookSkeleton`` is written to ``work/skeleton.json``.
    """
    if not state.get("chapter_sources"):
        msg = "build_skeleton requires chapter_sources; run split before build_skeleton"
        raise ValueError(msg)

    chapter_sources: dict[str, str] = state["chapter_sources"]
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})
    _LOG.info("build_skeleton: chapters=%d", len(chapter_sources))
    chapter_order = list(chapter_sources.keys())
    model = cfg.model_for("skeleton")
    cache_results: list[CacheResult] = []

    # -- Pass 1: per-chapter parallel candidate extraction (source chunked first) --
    semaphore = asyncio.Semaphore(cfg.chapter_concurrency)

    async def extract_chapter(
        ch_id: str, rel_source: str
    ) -> tuple[str, str, list[str], list[CacheResult]]:
        async with semaphore:
            source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
            title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
            topics = list(topics_by_chapter.get(ch_id, []))
            results: list[CacheResult] = []
            for chunk in chunk_by_heading(source_md, model=model, stage="skeleton"):
                results.append(
                    await run_with_cache(
                        SkeletonExtractAgent,
                        {
                            "chapter_id": ch_id,
                            "title": title,
                            "topics": topics,
                            "source_md": chunk.text,
                            "source_refs": chunk.source_refs,
                            "language": cfg.language,
                            "book_notes": cfg.book_notes,
                        },
                        model=model,
                        cache_dir=_cache_dir(cfg),
                        runtime=cfg.llm_runtime,
                    )
                )
            return ch_id, title, topics, results

    extraction = await asyncio.gather(
        *(extract_chapter(ch_id, rel) for ch_id, rel in chapter_sources.items())
    )

    candidates_by_chapter: dict[str, list[dict[str, Any]]] = {}
    titles_by_chapter: dict[str, str] = {}
    topics_resolved: dict[str, list[str]] = {}
    for ch_id, title, topics, results in extraction:
        cache_results.extend(results)
        titles_by_chapter[ch_id] = title
        topics_resolved[ch_id] = topics
        seen: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for result in results:
            for cand in result.result.candidates:
                key = _concept_key(cand.name)
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(cand.model_dump(mode="json"))
        candidates_by_chapter[ch_id] = candidates

    # -- Pass 2: serial fold (each call sees only candidates + the compact registry) --
    registry = Registry()
    for index, ch_id in enumerate(chapter_order):
        fold = await run_with_cache(
            SkeletonFoldAgent,
            {
                "chapter_id": ch_id,
                "chapter_title": titles_by_chapter.get(ch_id, ch_id),
                "chapter_order": index,
                "candidates": candidates_by_chapter.get(ch_id, []),
                "registry": registry.compact(),
                "language": cfg.language,
                "book_notes": cfg.book_notes,
            },
            model=model,
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.append(fold)
        registry.apply(fold.result.ops, current_chapter=ch_id)
        registry.record_uses(ch_id, fold.result.uses)

    chapter_briefs = {
        ch_id: _brief_for(titles_by_chapter.get(ch_id, ch_id), topics_resolved.get(ch_id, []))
        for ch_id in chapter_order
    }
    skeleton = registry.to_skeleton(chapter_briefs=chapter_briefs, chapter_order=chapter_order)
    out_path = write_json(
        cfg.work_dir / "skeleton.json",
        _agent_result_payload(SkeletonFoldAgent, model, skeleton),
    )
    glossary_len = len(skeleton.glossary)
    _LOG.info(
        "build_skeleton: wrote %s glossary=%d cache_hit=%s",
        _rel(out_path, cfg.book_dir),
        glossary_len,
        _stage_cache_hit(cache_results),
    )
    return {
        "skeleton": _rel(out_path, cfg.book_dir),
        "cache_hit": _stage_cache_hit(cache_results),
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

    Each section generator (``SectionPlannerAgent`` / ``SectionAgent``) receives only a
    **per-chapter slice**, not the whole book's terminology:

    - ``chapter_owns``: concepts whose ``first_chapter_id`` equals ``ch_id`` (this chapter
      owns the definition).
    - ``chapter_uses``: concepts owned by other chapters; only reference, never redefine.
    - ``alias_map_slice``: variant → canonical for *only* the concepts this chapter owns or
      uses (so the section payload's size grows with the chapter, not the whole book). The
      full ``alias_map`` is intentionally NOT shipped here — the integrator still rewrites
      every ``[[alias]]`` deterministically at render time; this slice is the writing-time
      terminology anchor that converts term drift inside prose the integrator cannot catch.
    - ``prev_brief`` / ``next_brief``: neighbouring chapter one-liners for transitions.
    """
    if not skeleton:
        return {}
    glossary = skeleton.get("glossary", []) or []
    full_alias_map = skeleton.get("alias_map", {}) or {}
    chapter_briefs = skeleton.get("chapter_briefs", {}) or {}
    chapter_order: list[str] = list(skeleton.get("chapter_order", []) or [])
    recorded_uses = skeleton.get("chapter_uses", {}) or {}

    owns: list[dict[str, Any]] = []
    not_owned: dict[str, dict[str, Any]] = {}
    for entry in glossary:
        if not isinstance(entry, dict):
            continue
        first = str(entry.get("first_chapter_id") or "")
        projected = {
            "canonical": entry.get("canonical"),
            "aliases": entry.get("aliases", []),
            "first_chapter_id": first,
        }
        if first == ch_id:
            owns.append(projected)
        elif entry.get("canonical"):
            not_owned[_concept_key(str(entry.get("canonical")))] = projected

    # ``chapter_uses`` from the fold names only the concepts this chapter actually
    # references, so the slice shrinks to the chapter. Skeletons without it (legacy /
    # hand-built) fall back to "every concept another chapter owns".
    if ch_id in recorded_uses:
        used_keys = {_concept_key(str(name)) for name in recorded_uses[ch_id]}
        uses = [not_owned[key] for key in not_owned if key in used_keys]
    else:
        uses = list(not_owned.values())

    # Slice the alias map to only the concepts this chapter owns or uses.
    slice_canonicals = {
        str(item.get("canonical")) for item in (*owns, *uses) if item.get("canonical")
    }
    alias_map_slice = {
        variant: canonical
        for variant, canonical in full_alias_map.items()
        if canonical in slice_canonicals
    }

    prev_brief = ""
    next_brief = ""
    if ch_id in chapter_order:
        position = chapter_order.index(ch_id)
        if position > 0:
            prev_brief = chapter_briefs.get(chapter_order[position - 1], "")
        if position + 1 < len(chapter_order):
            next_brief = chapter_briefs.get(chapter_order[position + 1], "")

    return {
        "chapter_owns": owns,
        "chapter_uses": uses,
        "alias_map_slice": alias_map_slice,
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
    targets = cfg.target_chapter_ids
    _LOG.info(
        "generate: chapters=%d concurrency=%d targets=%s",
        len(chapter_items),
        cfg.chapter_concurrency,
        sorted(targets) if targets else "all",
    )

    async def run_chapter(ch_id: str, rel_source: str):
        async with semaphore:
            source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
            title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
            _LOG.info("generate: chapter start ch_id=%s", ch_id)
            try:
                result = await generate_chapter_sections(
                    cfg=cfg,
                    chapter_id=ch_id,
                    title=title,
                    source_md=source_md,
                    source_path=rel_source,
                    topics=list(topics_by_chapter.get(ch_id, [])),
                    figures=_source_figures(source_md),
                    skeleton_payload=_skeleton_payload(skeleton_data, ch_id),
                )
            except Exception:
                _LOG.error("generate: chapter failed ch_id=%s", ch_id)
                raise
            _LOG.info(
                "generate: chapter done ch_id=%s cache_hit=%s issues=%d figures=%d",
                ch_id,
                result.cache_hit,
                len(result.issues),
                len(result.generated_figures),
            )
            return result

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

    _LOG.info(
        "generate: done ok=%d failed=%d issues=%d figures=%d cache_hit=%s",
        len(chapter_results),
        len(failures),
        len(generation_issues),
        len(generated_figures),
        bool(chapter_cache_hits) and all(chapter_cache_hits),
    )
    return {
        "agent_results": chapter_results,
        "generation_issues": generation_issues,
        "generated_figures": generated_figures,
        "generated_figures_index": _persist_generated_figures(cfg, generated_figures),
        "cache_hit": bool(chapter_cache_hits) and all(chapter_cache_hits),
    }


def prepare_generate_fanout_node(state: State, cfg: BookConfig) -> State:
    if not state.get("chapter_sources"):
        msg = "generate requires chapter_sources; run split before generate"
        raise ValueError(msg)
    missing = sorted(cfg.target_chapter_ids - set(state.get("chapter_sources", {})))
    if missing:
        msg = f"generate target chapter(s) not found in split output: {missing}"
        raise ValueError(msg)
    available = list(state.get("chapter_sources", {}))
    targets = cfg.target_chapter_ids
    ensure_dir(cfg.work_dir / "agent_results")
    _LOG.info(
        "prepare_generate: available=%d targets=%s",
        len(available),
        sorted(targets) if targets else "all",
    )
    return {"_generate_parts": None, "cache_hit": False}


def generate_fanout_specs(state: State, cfg: BookConfig) -> list[State]:
    targets = cfg.target_chapter_ids
    return [
        {
            "chapter_sources": state.get("chapter_sources", {}),
            "chapter_titles": state.get("chapter_titles", {}),
            "chapter_topics": state.get("chapter_topics", {}),
            "skeleton": state.get("skeleton"),
            "_fanout_chapter_id": ch_id,
            "_fanout_chapter_source": rel_source,
        }
        for ch_id, rel_source in state.get("chapter_sources", {}).items()
        if not targets or ch_id in targets
    ]


def _persist_generated_figures(
    cfg: BookConfig, generated_figures: dict[str, dict[str, str]]
) -> str:
    path = write_json(cfg.work_dir / "generated_figures.json", generated_figures)
    return _rel(path, cfg.book_dir)


async def generate_chapter_fanout_node(state: State, cfg: BookConfig) -> State:
    ch_id = str(state["_fanout_chapter_id"])
    rel_source = str(state["_fanout_chapter_source"])
    semaphore = _fanout_semaphores.setdefault(
        (id(cfg), "chapter"), asyncio.Semaphore(cfg.chapter_concurrency)
    )
    async with semaphore:
        _LOG.info("generate: chapter start ch_id=%s", ch_id)
        try:
            generated = await _run_generate_chapter_unit(state, cfg, ch_id, rel_source)
        except Exception:
            # Let the failure propagate. LangGraph records the *successful* siblings'
            # writes as pending writes against the pre-fanout checkpoint and leaves the
            # super-step uncommitted, so a later ``--resume`` re-runs only this chapter —
            # swallowing the error into a part would advance the checkpoint past the
            # fanout and pin ``--resume`` on the stale error forever.
            _LOG.exception("generate failed for chapter %s", ch_id)
            raise
    _LOG.info(
        "generate: chapter done ch_id=%s cache_hit=%s issues=%d figures=%d",
        ch_id,
        generated.get("cache_hit"),
        len(generated.get("generation_issues", [])),
        len(generated.get("generated_figures", {})),
    )
    return {"_generate_parts": {ch_id: generated}}


async def _run_generate_chapter_unit(
    state: State, cfg: BookConfig, ch_id: str, rel_source: str
) -> dict[str, Any]:
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    titles = state.get("chapter_titles", {})
    topics_by_chapter = state.get("chapter_topics", {})
    skeleton_data = _load_skeleton(state, cfg)
    source_md = (cfg.book_dir / rel_source).read_text(encoding="utf-8")
    title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
    generated = await generate_chapter_sections(
        cfg=cfg,
        chapter_id=ch_id,
        title=title,
        source_md=source_md,
        source_path=rel_source,
        topics=list(topics_by_chapter.get(ch_id, [])),
        figures=_source_figures(source_md),
        skeleton_payload=_skeleton_payload(skeleton_data, ch_id),
    )
    section_model = cfg.model_for("section")
    application_quiz_model = cfg.model_for("application_quiz")
    card_model = cfg.model_for("card")
    summary_model = cfg.model_for("summary")
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
    exam_path = await _generate_chapter_exam(
        state, cfg, ch_id, title, source_md, generated.chapter.body_md, result_dir
    )
    if exam_path is not None:
        paths["exam"] = exam_path
    return {
        "chapter_id": ch_id,
        "agent_results": {name: _rel(path, cfg.book_dir) for name, path in paths.items()},
        "generation_issues": [issue.model_dump(mode="json") for issue in generated.issues],
        "generated_figures": dict(generated.generated_figures),
        "cache_hit": generated.cache_hit,
    }


_EXAM_CHAPTER_RE = re.compile(
    r"试卷|期中|期末|考试|真题|测验|exam|midterm|mid-term|final", re.IGNORECASE
)


def _exam_chapter_ids(state: State) -> set[str]:
    """Heuristic, soft detection of past-exam chapters by title / source name.

    Detection only decides which agent runs (walkthrough vs generated exam) and what feeds the
    exam pool; a miss simply means a chapter gets a normal generated exam, so a wrong guess is
    never fatal (see design §3.1).
    """

    titles = state.get("chapter_titles", {})
    sources = state.get("chapter_sources", {})
    ids: set[str] = set()
    for ch_id in sources:
        haystack = f"{ch_id} {titles.get(ch_id, '')} {sources.get(ch_id, '')}"
        if _EXAM_CHAPTER_RE.search(haystack):
            ids.add(str(ch_id))
    return ids


def _collect_exam_questions(summaries: list[SourceSummaryResult]) -> dict[str, Any]:
    """Flatten the questions of every ``is_exam`` summary for later per-chapter distribution."""
    questions = [
        question.model_dump(mode="json")
        for summary in summaries
        if summary.is_exam
        for question in summary.exam_questions
    ]
    return {"questions": questions}


def _chapter_exam_pool(state: State, cfg: BookConfig, ch_id: str) -> list[dict[str, Any]]:
    """This chapter's slice of the past-exam pool, mapped by concept overlap.

    Structure persisted every detected past-exam question to ``work/structure/exam-pool.json``;
    here ``build_exam_pools`` assigns each question to the single best-matching chapter (by the
    chapter's approved topics), and we hand this chapter its slice as ExamAgent 套路 reference.
    """

    path = cfg.work_dir / "structure" / "exam-pool.json"
    if not path.exists():
        return []
    raw = read_json(path).get("questions") or []
    if not raw:
        return []
    summary = SourceSummaryResult(
        source_id="_exam_pool",
        summary_md="",
        is_exam=True,
        exam_questions=[DetectedExamQuestion.model_validate(item) for item in raw],
    )
    chapter_topics = {cid: list(topics) for cid, topics in state.get("chapter_topics", {}).items()}
    pools = build_exam_pools([summary], chapter_topics)
    return [question.model_dump(mode="json") for question in pools.get(ch_id, [])]


async def _generate_chapter_exam(
    state: State,
    cfg: BookConfig,
    ch_id: str,
    title: str,
    source_md: str,
    chapter_body_md: str,
    result_dir: Path,
) -> Path | None:
    """Generate the chapter's exam artifact: a walkthrough for past-paper chapters, otherwise a
    fresh chapter-end exam that borrows from any detected past papers. Returns the written path
    (added to the chapter's ``agent_results`` as ``exam``) or ``None`` on failure."""

    exam_ids = _exam_chapter_ids(state)
    allowed_refs = sorted(set(SOURCE_REF_RE.findall(source_md)))
    payload: dict[str, Any] = {
        "chapter_id": ch_id,
        "title": title,
        "source_md": source_md,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
        "chapter_body_md": chapter_body_md,
        "allowed_source_refs": allowed_refs,
    }
    if ch_id in exam_ids:
        agent_cls: type[Any] = ExamExplainAgent
        model = cfg.models.get("exam_explain") or cfg.model_for("application_quiz")
    else:
        agent_cls = ExamAgent
        model = cfg.models.get("exam") or cfg.model_for("application_quiz")
        payload["exam_pool"] = _chapter_exam_pool(state, cfg, ch_id)
    try:
        result = await run_with_cache(
            agent_cls,
            payload,
            model=model,
            cache_dir=cfg.cache_dir / "tasks",
            runtime=cfg.llm_runtime,
        )
    except Exception as exc:  # noqa: BLE001 - exam is additive; never wedge chapter generation.
        _LOG.warning("exam generation failed ch_id=%s: %s", ch_id, exc)
        return None
    return write_json(
        result_dir / f"{ch_id}.exam.json",
        _agent_result_payload(agent_cls, model, result.result),
    )


def collect_generate_fanout_node(state: State, cfg: BookConfig) -> State:
    parts = state.get("_generate_parts") or {}
    targets = cfg.target_chapter_ids
    # Carry forward prior results ONLY for chapters this run isn't regenerating — i.e.
    # only on a *targeted* run. A full run (no targets) fans out every chapter, so the
    # fresh parts below are authoritative; seeding from ``state`` here would resurrect
    # stale entries left in the channel by a structure change or an earlier run, whose
    # files ``prepare_generate`` may have already cleared.
    chapter_results: dict[str, dict[str, str]] = {
        str(ch_id): dict(paths)
        for ch_id, paths in state.get("agent_results", {}).items()
        if targets and str(ch_id) not in targets
    }
    chapter_cache_hits: list[bool] = []
    generation_issues: list[dict[str, Any]] = [
        dict(issue)
        for issue in state.get("generation_issues", [])
        if targets and _issue_chapter_id(issue) not in targets
    ]
    generated_figures: dict[str, dict[str, str]] = {
        str(ch_id): dict(figures)
        for ch_id, figures in state.get("generated_figures", {}).items()
        if targets and str(ch_id) not in targets
    }
    # A failed chapter worker raises rather than returning a part, so the collect node
    # only ever runs once *every* fanned-out chapter produced a result (LangGraph aborts
    # the super-step otherwise). A missing part therefore signals a real invariant break,
    # not a generation failure — surface it loudly.
    missing: list[str] = []
    chapter_ids = [
        ch_id for ch_id in state.get("chapter_sources", {}) if not targets or ch_id in targets
    ]
    for ch_id in chapter_ids:
        part = parts.get(ch_id)
        if not part or "agent_results" not in part:
            missing.append(ch_id)
            continue
        chapter_results[ch_id] = dict(part["agent_results"])
        chapter_cache_hits.append(bool(part.get("cache_hit", False)))
        generation_issues.extend(part.get("generation_issues", []))
        figures = part.get("generated_figures") or {}
        if figures:
            generated_figures[ch_id] = dict(figures)

    if missing:
        raise RuntimeError(f"generate produced no fanout result for chapters: {missing}")

    _LOG.info(
        "collect_generate: ok=%d issues=%d figures=%d cache_hit=%s",
        len(chapter_results),
        len(generation_issues),
        len(generated_figures),
        bool(chapter_cache_hits) and all(chapter_cache_hits),
    )
    return {
        "agent_results": chapter_results,
        "generation_issues": generation_issues,
        "generated_figures": generated_figures,
        "generated_figures_index": _persist_generated_figures(cfg, generated_figures),
        "cache_hit": bool(chapter_cache_hits) and all(chapter_cache_hits),
    }


def _issue_chapter_id(issue: dict[str, Any]) -> str | None:
    owner = str(issue.get("owner_task_id") or "")
    if ":" not in owner:
        return None
    return owner.split(":", 1)[0]


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    agent_results = {
        str(ch_id): dict(paths) for ch_id, paths in state.get("agent_results", {}).items()
    }
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    cache_results: list[CacheResult] = []
    chapters_in = list(state.get("agent_results", {}).items())
    reused_concepts = sum(1 for _ch, p in chapters_in if "concepts" in p)
    extracted_concepts = 0
    _LOG.info(
        "reconcile: chapters=%d with_cached_concepts=%d",
        len(chapters_in),
        reused_concepts,
    )
    for ch_id, paths in chapters_in:
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
        extracted_concepts += 1

    skeleton = _load_skeleton(state, cfg)
    if skeleton is not None:
        _LOG.info(
            "reconcile: using skeleton merge candidates=%d extracted_chapters=%d",
            len(candidates),
            extracted_concepts,
        )
        reconciled_model = _merge_candidates_with_skeleton(skeleton, candidates)
    else:
        _LOG.info(
            "reconcile: skeleton absent, falling back to LLM reconcile candidates=%d",
            len(candidates),
        )
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
    _LOG.info(
        "reconcile: done concepts=%d alias_map=%d cache_hit=%s",
        len(reconciled_model.concepts),
        len(reconciled_model.alias_map),
        _stage_cache_hit(cache_results),
    )
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
    glossary_names = [
        str(c.get("canonical")) for c in data.get("concepts", []) if c.get("canonical")
    ]
    concepts = list(data.get("concepts", []))
    _LOG.info("concept_pages: concepts=%d", len(concepts))
    for index, item in enumerate(concepts, start=1):
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
        _LOG.info(
            "concept_pages: [%d/%d] name=%s cache_hit=%s issue=%s",
            index,
            len(concepts),
            concept.name,
            result.cache_hit,
            bool(inline_issue),
        )
    _LOG.info(
        "concept_pages: done wrote=%d issues=%d cache_hit=%s",
        len(outputs),
        len(concept_generation_issues),
        _stage_cache_hit(cache_results),
    )
    return {
        "concept_pages": outputs,
        "concept_generation_issues": concept_generation_issues,
        "cache_hit": _stage_cache_hit(cache_results),
    }


def prepare_concept_pages_fanout_node(state: State, cfg: BookConfig) -> State:
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    targets = cfg.target_concept_names
    if targets:
        data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
        available = {_concept_item_name(item) for item in data.get("concepts", [])}
        missing = sorted(targets - available)
        if missing:
            msg = f"concept_pages target concept(s) not found in reconciled concepts: {missing}"
            raise ValueError(msg)
        _LOG.info(
            "prepare_concept_pages: targets=%d available=%d",
            len(targets),
            len(available),
        )
    else:
        _clear_generated_files(out_dir, "*.json")
        data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
        _LOG.info(
            "prepare_concept_pages: clearing outputs, available=%d",
            len(data.get("concepts", [])),
        )
    return {"_concept_page_parts": None, "cache_hit": False}


def concept_page_fanout_specs(state: State, cfg: BookConfig) -> list[State]:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    glossary_names = [
        str(c.get("canonical")) for c in data.get("concepts", []) if c.get("canonical")
    ]
    used_stems: set[str] = set()
    specs: list[State] = []
    targets = cfg.target_concept_names
    for order, item in enumerate(data.get("concepts", [])):
        name = _concept_item_name(item)
        safe_name = _unique_file_stem(name, used_stems, fallback_prefix="concept")
        if targets and name not in targets:
            continue
        specs.append(
            {
                "agent_results": state.get("agent_results", {}),
                "chapter_sources": state.get("chapter_sources", {}),
                "reconciled_concepts": state.get("reconciled_concepts"),
                "_fanout_concept_order": order,
                "_fanout_concept_item": item,
                "_fanout_concept_glossary": glossary_names,
                "_fanout_concept_stem": safe_name,
            }
        )
    return specs


def _concept_item_name(item: dict[str, Any]) -> str:
    return str(item.get("canonical") or item.get("name") or "concept").strip()


async def concept_page_fanout_node(state: State, cfg: BookConfig) -> State:
    item = dict(state["_fanout_concept_item"])
    order = int(state["_fanout_concept_order"])
    name = str(item.get("canonical") or item.get("name") or f"concept-{order}")
    semaphore = _fanout_semaphores.setdefault(
        (id(cfg), "concept"), asyncio.Semaphore(cfg.chapter_concurrency)
    )
    async with semaphore:
        _LOG.info("concept_page: start name=%s order=%d", name, order)
        try:
            part = await _run_concept_page_unit(state, cfg, item, order)
        except Exception:
            # Propagate so LangGraph re-runs only this concept on ``--resume`` (see the
            # generate worker for the full rationale).
            _LOG.exception("concept page failed for %s", name)
            raise
    _LOG.info(
        "concept_page: done name=%s cache_hit=%s issue=%s",
        name,
        part.get("cache_hit"),
        bool(part.get("concept_generation_issues")),
    )
    return {"_concept_page_parts": {part["name"]: part}}


async def _run_concept_page_unit(
    state: State, cfg: BookConfig, item: dict[str, Any], order: int
) -> dict[str, Any]:
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    glossary_names = list(state.get("_fanout_concept_glossary", []))
    chapter_contexts = _concept_contexts(item, state, cfg)
    concept_input = {
        **item,
        "chapter_contexts": chapter_contexts,
        "glossary": glossary_names,
        "language": cfg.language,
        "book_notes": cfg.book_notes,
    }
    cache_results: list[CacheResult] = []
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
    safe_name = str(state["_fanout_concept_stem"])
    path = write_json(out_dir / f"{safe_name}.json", _json_model(concept))
    issues = [inline_issue.model_dump(mode="json")] if inline_issue is not None else []
    return {
        "name": concept.name,
        "order": order,
        "path": _rel(path, cfg.book_dir),
        "concept_generation_issues": issues,
        "cache_hit": _stage_cache_hit(cache_results),
    }


def collect_concept_pages_fanout_node(state: State, cfg: BookConfig) -> State:
    parts = state.get("_concept_page_parts") or {}
    targets = cfg.target_concept_names
    # Carry prior pages only on a targeted run (see the generate collect). A full run
    # regenerates every reconciled concept, so seeding from ``state`` would resurrect
    # stale entries whose files ``prepare_concept_pages`` already cleared.
    outputs: dict[str, str] = {
        str(name): str(path)
        for name, path in state.get("concept_pages", {}).items()
        if targets and str(name) not in targets
    }
    concept_generation_issues: list[dict[str, Any]] = [
        dict(issue)
        for issue in state.get("concept_generation_issues", [])
        if targets and _issue_concept_name(issue) not in targets
    ]
    cache_hits: list[bool] = []

    # A failed concept worker raises (aborting the super-step) instead of returning an
    # error part, so every part reaching collect is a success. See the generate collect.
    ordered = sorted(parts.values(), key=lambda part: int(part.get("order", 0)))
    for part in ordered:
        name = str(part.get("name") or "concept")
        if "path" not in part:  # a legacy swallowed-error part — heal via resume rewind
            raise RuntimeError(f"concept_pages produced no fanout result for: {name}")
        outputs[name] = str(part["path"])
        concept_generation_issues.extend(part.get("concept_generation_issues", []))
        cache_hits.append(bool(part.get("cache_hit", False)))

    _LOG.info(
        "collect_concept_pages: ok=%d issues=%d cache_hit=%s",
        len(outputs),
        len(concept_generation_issues),
        bool(cache_hits) and all(cache_hits),
    )
    return {
        "concept_pages": outputs,
        "concept_generation_issues": concept_generation_issues,
        "cache_hit": bool(cache_hits) and all(cache_hits),
    }


def _issue_concept_name(issue: dict[str, Any]) -> str | None:
    owner = str(issue.get("owner_task_id") or "")
    if not owner.startswith("concept:"):
        return None
    return owner.split(":", 1)[1]


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


# Filename of a chapter's exam page. Exam pages are *structural* (a folder's ``index.mdx`` holds the
# teaching body, ``exam.mdx`` holds the exam) and legitimately carry no QuizBlock/Anki/Sources, so
# ``check_node`` keys off this name to exempt them from those pedagogical-section checks.
_EXAM_PAGE_FILENAME = "exam.mdx"


def _write_exam_page(cfg: BookConfig, chapter_dir: Path, exam_rel: str, display_title: str) -> None:
    """Render a chapter's exam artifact to ``<chapter>/exam.mdx`` and write the folder meta.

    ``mode`` is read off the owner task id (``:explain`` → past-paper walkthrough, else a
    generated chapter exam), so the same renderer drives both surfaces.
    """

    exam = ExamResult.model_validate(_agent_result(read_json(cfg.book_dir / exam_rel)))
    mode = "walkthrough" if exam.owner_task_id.endswith(":explain") else "exam"
    page_title = f"{display_title} · {'讲解' if mode == 'walkthrough' else '测验'}"
    write_text(
        chapter_dir / _EXAM_PAGE_FILENAME,
        _frontmatter({"title": page_title, "type": "chapter"}) + render_exam_mdx(exam, mode=mode),
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


def _site_typecheck_issues(cfg: BookConfig) -> list[Issue]:
    mode = str(cfg.generation.get("siteTypeCheck", "auto") or "auto").lower()
    if mode in {"off", "false", "0", "disabled"}:
        return []
    required = mode in {"required", "on", "true", "1"}
    if mode not in {"auto", "required", "on", "true", "1"}:
        _LOG.warning("unknown generation.siteTypeCheck=%s; treating as auto", mode)
        required = False

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        message = "site type check skipped: pnpm is unavailable"
        if required:
            return [
                Issue(
                    severity="error",
                    code="SITE_TYPECHECK_UNAVAILABLE",
                    message=message,
                    owner_task_id="site:typecheck",
                )
            ]
        _LOG.info("%s", message)
        return []

    # site is the single source of truth: integrate already scaffolded the framework and rendered
    # content into it, so check validates it in place — no per-round materialize. Reuse installed
    # deps (preserved across runs); only install when node_modules is genuinely absent.
    site_dir = cfg.site_dir
    env = _site_typecheck_env(cfg)
    if not (site_dir / "node_modules").exists():
        try:
            install_proc = subprocess.run(  # noqa: S603 - fixed argv, project-local package manager
                [pnpm, "install"],
                cwd=site_dir,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return [
                Issue(
                    severity="error",
                    code="SITE_TYPECHECK_ERROR",
                    message=f"site dependency install failed to run: {exc}",
                    owner_task_id="site:typecheck",
                )
            ]
        if install_proc.returncode != 0:
            output = _redact_site_typecheck_output(
                "\n".join(
                    part
                    for part in [install_proc.stdout.strip(), install_proc.stderr.strip()]
                    if part
                )
            )
            if len(output) > 4000:
                output = output[:4000] + "..."
            return [
                Issue(
                    severity="error",
                    code="SITE_TYPECHECK_ERROR",
                    message=(
                        f"site dependency install failed (exit {install_proc.returncode}): {output}"
                    ),
                    owner_task_id="site:typecheck",
                )
            ]

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, project-local package script
            [pnpm, "run", "types:check"],
            cwd=site_dir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [
            Issue(
                severity="error",
                code="SITE_TYPECHECK_ERROR",
                message=f"site type check failed to run: {exc}",
                owner_task_id="site:typecheck",
            )
        ]
    if proc.returncode != 0:
        output = _redact_site_typecheck_output(
            "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
        )
        if len(output) > 4000:
            output = output[:4000] + "..."
        return [
            Issue(
                severity="error",
                code="SITE_TYPECHECK_ERROR",
                message=f"site type check failed (exit {proc.returncode}): {output}",
                owner_task_id="site:typecheck",
            )
        ]
    # types:check passed → run a real build to surface runtime render errors (e.g. ShikiError on an
    # unknown code-fence language, component render failures) that a type-only check cannot see.
    return _site_build_issues(site_dir, env, pnpm)


def _site_build_issues(site_dir: Path, env: dict[str, str], pnpm: str) -> list[Issue]:
    """Run ``pnpm run build`` and report a SITE_BUILD_ERROR if the production build fails."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, project-local package script
            [pnpm, "run", "build"],
            cwd=site_dir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [
            Issue(
                severity="error",
                code="SITE_BUILD_ERROR",
                message=f"site build failed to run: {exc}",
                owner_task_id="site:build",
            )
        ]
    if proc.returncode == 0:
        return []
    output = _redact_site_typecheck_output(
        "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
    )
    if len(output) > 4000:
        output = output[:4000] + "..."
    return [
        Issue(
            severity="error",
            code="SITE_BUILD_ERROR",
            message=f"site build failed (exit {proc.returncode}): {output}",
            owner_task_id="site:build",
        )
    ]


def _site_typecheck_env(cfg: BookConfig) -> dict[str, str]:
    env: dict[str, str] = {
        "BOOKWIKI_SITE_LANGUAGE": cfg.language,
        "NODE_OPTIONS": "--max-old-space-size=4096",
    }
    for key in ("PATH", "HOME", "TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _redact_site_typecheck_output(output: str) -> str:
    redacted = output
    for key, value in os.environ.items():
        if _looks_sensitive_env_key(key) and value and len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _looks_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))


async def check_node(state: State, cfg: BookConfig) -> State:
    _require_mdx_validator(cfg)
    issues: list[Issue] = []
    for raw_issue in state.get("generation_issues", []):
        if isinstance(raw_issue, dict):
            issues.append(Issue.model_validate(raw_issue))
    for raw_issue in state.get("concept_generation_issues", []):
        if isinstance(raw_issue, dict):
            issues.append(Issue.model_validate(raw_issue))
    _LOG.info(
        "check: seed issues from generate=%d concept_pages=%d",
        len(state.get("generation_issues", [])),
        len(state.get("concept_generation_issues", [])),
    )
    chapter_mdx_files = sorted((cfg.content_dir / "chapters").rglob("*.mdx"))
    concept_mdx_files = sorted((cfg.content_dir / "concepts").glob("*.mdx"))
    if not (cfg.content_dir / "index.mdx").exists():
        issues.append(
            Issue(
                severity="error",
                code="MISSING_CONTENT_INDEX",
                message="content/docs/index.mdx was not generated",
                owner_task_id="content:index",
            )
        )
    chapter_texts = {path: path.read_text(encoding="utf-8") for path in chapter_mdx_files}
    # One Node process per batch instead of a cold start per file (~550 files → ~100s).
    chapter_mdx = validate_mdx_many(
        [(str(path), chapter_texts[path]) for path in chapter_mdx_files]
    )
    for path in chapter_mdx_files:
        text = chapter_texts[path]
        # owner_task_id carries the chapter-relative path (e.g. ``Chapter-19-X/index``) rather
        # than the bare stem: 30 chapters all share ``index.mdx``/``exam.mdx``, so a stem-based id
        # collapses them onto one target and ``_target_mdx_path`` would only ever repair the first.
        rel_id = path.relative_to(cfg.content_dir / "chapters").with_suffix("").as_posix()
        mdx_errors = chapter_mdx.get(str(path), [])
        for error in mdx_errors:
            issues.append(
                Issue(
                    severity="error",
                    code="MDX_PARSE_ERROR",
                    message=f"{path.name} fails MDX compilation: {error}",
                    owner_task_id=f"{rel_id}:chapter",
                )
            )
        issues.extend(_illegal_component_fence_issues(text, f"{rel_id}:chapter"))
        if not text.startswith("---\n"):
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_FRONTMATTER",
                    message=f"{path.name} has no YAML frontmatter",
                    owner_task_id=f"{rel_id}:chapter",
                )
            )
        # QuizBlock / Anki Cards / Sources are pedagogical sections that only teaching-chapter
        # pages carry. Exam pages (``exam.mdx``) are structural and legitimately omit them, so we
        # skip these checks there (otherwise every exam page is a permanent false positive).
        # The three are also reported as ``warning`` rather than ``error``: none has a deterministic
        # repair path (ReviewAgent only emits advice, nothing re-fills the missing section), so an
        # ``error`` would only burn futile repair rounds. We record them instead of trying to fix.
        if path.name != _EXAM_PAGE_FILENAME:
            if "<QuizBlock" not in text:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_QUIZ",
                        message=f"{path.name} has no QuizBlock",
                        owner_task_id=f"{rel_id}:quiz",
                    )
                )
            elif not mdx_errors:
                issues.extend(_inline_quiz_answer_issues(text, rel_id))
            if "## Anki Cards" not in text:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_ANKI",
                        message=f"{path.name} has no Anki Cards section",
                        owner_task_id=f"{rel_id}:card",
                    )
                )
            if "## Sources" not in text:
                issues.append(
                    Issue(
                        severity="warning",
                        code="MISSING_SOURCES",
                        message=f"{path.name} has no Sources section",
                        owner_task_id=f"{rel_id}:chapter",
                    )
                )
        for phrase in _suspicious_phrases(text):
            issues.append(
                Issue(
                    severity="warning",
                    code="SUSPICIOUS_INSTRUCTION",
                    message=f"{path.name} contains suspicious instruction text: {phrase}",
                    owner_task_id=f"{rel_id}:chapter",
                )
            )
        for target in re.findall(r"\]\((?!https?://|mailto:|#)([^)]+)\)", text):
            if not _mdx_link_exists(path.parent, target):
                issues.append(
                    Issue(
                        severity="error",
                        code="BROKEN_LINK",
                        message=f"{path.name} links to missing target {target}",
                        owner_task_id=f"{rel_id}:chapter",
                    )
                )

    concept_texts = {path: path.read_text(encoding="utf-8") for path in concept_mdx_files}
    concept_mdx = validate_mdx_many(
        [(str(path), concept_texts[path]) for path in concept_mdx_files]
    )
    for path in concept_mdx_files:
        for error in concept_mdx.get(str(path), []):
            issues.append(
                Issue(
                    severity="error",
                    code="MDX_PARSE_ERROR",
                    message=f"{path.name} fails MDX compilation: {error}",
                    owner_task_id=f"concept-mdx:{path.stem}",
                )
            )
        issues.extend(
            _illegal_component_fence_issues(concept_texts[path], f"concept-mdx:{path.stem}")
        )

    allowed_refs = _allowed_source_refs(state, cfg)
    _LOG.info(
        "check: chapter_mdx=%d concept_mdx=%d allowed_refs=%d",
        len(chapter_mdx_files),
        len(concept_mdx_files),
        len(allowed_refs),
    )
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
    issues.extend(_site_typecheck_issues(cfg))
    status = "needs_repair" if issues else "passed"
    report = CheckReport(status=status, issues=issues)
    logs_dir = ensure_dir(cfg.work_dir / "logs")
    report_path = write_json(logs_dir / "check-report.json", report.model_dump(mode="json"))
    write_json(cfg.work_dir / "check-report.json", report.model_dump(mode="json"))
    write_text(logs_dir / "check-report.md", _render_check_report_md(report))
    by_severity: dict[str, int] = {}
    for issue in issues:
        key = str(issue.severity)
        by_severity[key] = by_severity.get(key, 0) + 1
    _LOG.info(
        "check: done status=%s issues=%d by_severity=%s report=%s",
        status,
        len(issues),
        by_severity,
        _rel(report_path, cfg.book_dir),
    )
    return {
        "check_report": _rel(report_path, cfg.book_dir),
        "repair_targets": report.repair_targets,
    }


async def repair_node(state: State, cfg: BookConfig) -> State:
    targets = state.get("repair_targets", [])
    if not targets:
        _LOG.info("repair: no targets, nothing to do")
        return {"repair_targets": []}
    rounds = dict(state.get("_repair_rounds", {}))
    out_dir = ensure_dir(cfg.work_dir / "repairs")
    outputs = []
    repair_actions: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    report = read_json(cfg.book_dir / state.get("check_report", "work/logs/check-report.json"))
    _LOG.info(
        "repair: targets=%d rounds_state=%d max_rounds=%d",
        len(targets),
        len(rounds),
        int(cfg.generation.get("maxRepairRounds", 1) or 1),
    )
    mdx_repaired = 0
    review_repaired = 0
    applied = 0
    mdx_edited: list[str] = []
    for target in targets:
        target_issues = [
            issue for issue in report.get("issues", []) if issue.get("owner_task_id") == target
        ]
        codes = {str(issue.get("code")) for issue in target_issues}
        max_rounds = _repair_round_limit(codes, cfg)
        if int(rounds.get(target, 0)) >= max_rounds:
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
        route = "mdx" if codes & _MDX_ROUTE_CODES else "review"
        _LOG.info(
            "repair: target=%s route=%s codes=%s round=%d/%d",
            target,
            route,
            sorted(codes),
            rounds[target],
            max_rounds,
        )
        if codes & _MDX_ROUTE_CODES:
            # Edit the rendered ``.mdx`` in place; the route below goes back to ``check`` (not
            # ``integrate``), so the edit is validated directly without being re-rendered away.
            if await _repair_mdx_file(target, target_issues, state, cfg):
                mdx_edited.append(target)
                mdx_repaired += 1
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
            review_repaired += 1
        action = _apply_repair(target, target_issues, state, cfg)
        if action is not None:
            repair_actions.append(action)
            applied += 1
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
    _LOG.info(
        "repair: done outputs=%d mdx_repaired=%d review_repaired=%d "
        "destructive_applied=%d exhausted=%d",
        len(outputs),
        mdx_repaired,
        review_repaired,
        applied,
        len(exhausted),
    )
    return {
        "repairs": outputs,
        "mdx_edited": mdx_edited,
        # Review/destructive repairs change source artifacts and need ``integrate`` to
        # re-render; in-place ``.mdx`` edits only need ``check`` to re-validate.
        "repair_artifact_changed": bool(outputs or repair_actions),
        "repair_targets": [],
        "_repair_rounds": rounds,
        "repair_exhausted": exhausted,
    }


def _repair_round_limit(codes: set[str], cfg: BookConfig) -> int:
    del codes
    return int(cfg.generation.get("maxRepairRounds", 1) or 1)


def _target_mdx_path(target: str, cfg: BookConfig) -> Path | None:
    """Map a ``check`` ``owner_task_id`` back to the rendered ``.mdx`` file it validated.

    ``check`` derives ``<chapter-rel-path>:<kind>`` from each chapter file (e.g.
    ``Chapter-19-X/index:chapter``) and ``concept-mdx:<stem>`` from each concept file, so the
    reverse mapping joins that relative path under ``chapters/``. The relative path is unique per
    file, which is what makes per-file repair possible (a bare stem would alias all ``index.mdx``).
    """
    if target.startswith("concept-mdx:"):
        path = cfg.content_dir / "concepts" / f"{target.partition(':')[2]}.mdx"
        return path if path.exists() else None
    rel_id = target.rsplit(":", 1)[0]
    path = cfg.content_dir / "chapters" / f"{rel_id}.mdx"
    return path if path.exists() else None


async def _repair_mdx_file(
    target: str, target_issues: list[dict[str, Any]], state: State, cfg: BookConfig
) -> bool:
    """Fix MDX compile errors by editing the rendered ``.mdx`` file IN PLACE.

    Feeds the actual ``.mdx`` bytes (the ones ``check`` compiled) plus the ``MDX_PARSE_ERROR``
    diagnostics to ``MdxEditRepairAgent``, then writes the repaired text back. The caller
    routes to ``check`` (NOT ``integrate``) so the edit is not clobbered by re-rendering;
    on a later ``integrate`` the file is regenerated from source and re-repaired. Returns
    whether the file changed.
    """
    path = _target_mdx_path(target, cfg)
    if path is None:
        return False
    text = path.read_text(encoding="utf-8")
    codes = {str(issue.get("code")) for issue in target_issues}
    changed = False
    # Deterministically unwrap component-wrapping code fences (e.g. ```quiz around <QuizBlock>)
    # before handing anything to the LLM — this needs no model and never touches the component.
    if "ILLEGAL_CODE_FENCE" in codes:
        unwrapped = _strip_illegal_component_fences(text)
        if unwrapped != text:
            text = unwrapped
            changed = True
    mdx_errors = [
        str(issue.get("message"))
        for issue in target_issues
        if str(issue.get("code")) == "MDX_PARSE_ERROR"
    ]
    if mdx_errors:
        result = await run_with_cache(
            MdxEditRepairAgent,
            {"mdx": text, "mdx_errors": mdx_errors, "language": cfg.language, "doc_label": target},
            model=cfg.model_for("mdx_repair"),
            cache_dir=_cache_dir(cfg),
            force=True,
            runtime=cfg.llm_runtime,
        )
        repaired = result.result.mdx
        if repaired != text:
            text = repaired
            changed = True
    if not changed:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    _LOG.info("index: building sqlite db=%s", _rel(db_path, cfg.book_dir))
    build_sqlite_index(cfg.content_dir, db_path)
    size = db_path.stat().st_size if db_path.exists() else 0
    _LOG.info("index: done db_size_bytes=%d", size)
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
