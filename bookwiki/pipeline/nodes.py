from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from bookwiki.agents import (
    ChapterSplitAgent,
    ConceptAgent,
    ConceptExtractAgent,
    ConceptReconcileAgent,
    LessonAgent,
    ReviewAgent,
    SourceLayoutRepairAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
    VisionCaptionAgent,
)
from bookwiki.convert.common import source_id_from_stem
from bookwiki.convert.mineru_client import convert_document_to_source
from bookwiki.convert.source_normalizer import NormalizedSource, normalize_structured_source
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.indexer.sqlite_builder import build_sqlite_index
from bookwiki.integrator.markdown_renderers import normalize_mdx_math
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.split.chapter_splitter import parse_approved_structure
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_text

State = dict[str, Any]

APPROVED_STRUCTURE_MARKER = "# bookwiki: approved-structure"
PENDING_STRUCTURE_MARKER = "# bookwiki: pending-structure-review"


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
    return normalize_mdx_math(quote)


def _display_chapter_title(chapter_id: str, title: str) -> str:
    clean = str(title).strip()
    if re.match(r"^(chapter\s+\d+\b|第\s*\d+\s*章)", clean, flags=re.IGNORECASE):
        return clean
    match = re.fullmatch(r"chapter-(\d+)", str(chapter_id)) or re.fullmatch(
        r"ch0*(\d+)", str(chapter_id)
    )
    if match:
        prefix = f"Chapter {int(match.group(1))}"
        return f"{prefix} {clean}".strip()
    return clean or str(chapter_id)


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
        part if part.startswith(("`", "$")) else _escape_mdx_text_segment(part)
        for part in parts
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
    return "\n".join(
        [
            f"<QuizItem {props}>",
            _mdx_child("QuizQuestion", item.get("question", "")),
            "<QuizChoices>",
            choice_mdx,
            "</QuizChoices>",
            "<QuizCheck />",
            _mdx_child("QuizExplanation", item.get("explanation", "")),
            "</QuizItem>",
        ]
    )


def _quiz_items_mdx(items: list[dict[str, Any]], item_indexes: list[int] | None = None) -> str:
    rendered: list[str] = []
    indexes = item_indexes if item_indexes is not None else list(range(1, len(items) + 1))
    for item, index in zip(items, indexes, strict=False):
        rendered.append(_quiz_item_mdx(item, index))
    return "\n\n".join(rendered)


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


def _quiz_block_mdx(
    title: str, items: list[dict[str, Any]], item_indexes: list[int] | None = None
) -> str:
    return (
        f"## {title or 'Quiz'}\n\n<QuizBlock>\n"
        f"{_quiz_items_mdx(items, item_indexes)}\n</QuizBlock>"
    )


def _insert_quiz_blocks(body_md: str, quiz: dict[str, Any]) -> str:
    heading, content_md = _split_leading_h1(body_md)
    blocks = [block.strip() for block in re.split(r"\n{2,}", content_md.strip()) if block.strip()]
    items = [item for item in quiz.get("items", []) if isinstance(item, dict)]
    placements = _quiz_placements_for_render(quiz, len(items))
    if not items:
        return _join_leading_h1(heading, content_md.strip())
    if not placements:
        quiz_block = _quiz_block_mdx("Quiz", items)
        if len(blocks) < 2:
            return _join_leading_h1(heading, f"{content_md.strip()}\n\n{quiz_block}".strip())
        insert_after = max(1, (len(blocks) + 1) // 2)
        merged = [*blocks[:insert_after], quiz_block, *blocks[insert_after:]]
        return _join_leading_h1(heading, "\n\n".join(merged))

    chunks_by_after: dict[int, list[str]] = {}
    used_indexes: set[int] = set()
    for placement in placements:
        indexes = [
            index
            for index in placement["item_indexes"]
            if 1 <= index <= len(items) and index not in used_indexes
        ]
        if not indexes:
            continue
        used_indexes.update(indexes)
        selected = [items[index - 1] for index in indexes]
        after = min(max(int(placement["after_block"]), 0), max(len(blocks) - 1, 0))
        chunks_by_after.setdefault(after, []).append(
            _quiz_block_mdx(str(placement.get("title") or "Quiz"), selected, indexes)
        )

    unassigned_indexes = [
        index for index in range(1, len(items) + 1) if index not in used_indexes
    ]
    unassigned = [items[index - 1] for index in unassigned_indexes]
    if unassigned:
        after = max(chunks_by_after, default=max(0, (len(blocks) - 1) // 2))
        chunks_by_after.setdefault(after, []).append(
            _quiz_block_mdx("Quiz", unassigned, unassigned_indexes)
        )

    merged: list[str] = []
    for index, block in enumerate(blocks):
        merged.append(block)
        if index in chunks_by_after:
            merged.extend(chunks_by_after[index])
    if not blocks:
        merged.extend(chunks_by_after.get(0, []))
    return _join_leading_h1(heading, "\n\n".join(merged))


def _split_leading_h1(markdown: str) -> tuple[str, str]:
    lines = str(markdown).strip().splitlines()
    if lines and re.match(r"^#\s+.+$", lines[0]):
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    return "", str(markdown).strip()


def _join_leading_h1(heading: str, markdown: str) -> str:
    body = str(markdown).strip()
    if heading and body:
        return f"{heading}\n\n{body}"
    return heading or body


def _quiz_placements_for_render(quiz: dict[str, Any], item_count: int) -> list[dict[str, Any]]:
    placements: list[dict[str, Any]] = []
    for raw in quiz.get("placements", []):
        if not isinstance(raw, dict):
            continue
        item_indexes = []
        for value in raw.get("item_indexes", []):
            try:
                item_indexes.append(int(value))
            except (TypeError, ValueError):
                continue
        if not item_indexes and item_count:
            continue
        try:
            after_block = int(raw.get("after_block", 0))
        except (TypeError, ValueError):
            after_block = 0
        placements.append(
            {
                "after_block": after_block,
                "item_indexes": item_indexes,
                "title": str(raw.get("title") or "Quiz"),
            }
        )
    return placements


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


def _normalize_concept_links(
    markdown: str, alias_map: dict[str, str], concept_previews: dict[str, dict[str, str]]
) -> str:
    linked_canonicals: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        canonical = alias_map.get(label) or alias_map.get(_concept_key(label)) or label
        preview = concept_previews.get(canonical)
        if preview:
            linked_canonicals.add(canonical)
            return _preview_link_mdx(
                preview["href"], preview["title"], preview["summary"], canonical
            )
        return f"[[{canonical}]]"

    normalized = re.sub(r"\[\[([^\]]+)\]\]", replace, markdown)
    return _auto_link_concept_terms(
        normalized, _concept_link_terms(alias_map, concept_previews), linked_canonicals
    )


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


def _concept_contexts(
    item: dict[str, Any], state: State, cfg: BookConfig
) -> list[dict[str, Any]]:
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
) -> None:
    path = _owner_artifact_path(owner_task_id, state, cfg)
    if path is None:
        return
    payload = read_json(path)
    result = payload.get("result", payload)
    codes = {str(issue.get("code")) for issue in issues}
    allowed_refs = _allowed_source_refs(state, cfg)
    if "UNKNOWN_SOURCE_REF" in codes and allowed_refs:
        _replace_invalid_citation_refs(result, allowed_refs, sorted(allowed_refs)[0])
    _, _, kind = owner_task_id.partition(":")
    if kind == "quiz" and "QUIZ_ANSWER_NOT_IN_CHOICES" in codes:
        for item in result.get("items", []):
            choices = [str(choice) for choice in item.get("choices", [])]
            if choices and str(item.get("answer", "")) not in choices:
                item["answer"] = choices[0]
    elif kind == "card" and "EMPTY_CARD_SIDE" in codes:
        for index, item in enumerate(result.get("items", []), start=1):
            if not str(item.get("front", "")).strip():
                item["front"] = f"Card {index}"
            if not str(item.get("back", "")).strip():
                item["back"] = "Review the source material for this card."
    write_json(path, payload)


def _replace_invalid_citation_refs(value: Any, allowed_refs: set[str], replacement: str) -> None:
    if isinstance(value, dict):
        if "ref_id" in value and "quote" in value and str(value["ref_id"]) not in allowed_refs:
            value["ref_id"] = replacement
            if not str(value.get("quote", "")).strip():
                value["quote"] = "source context"
        for item in value.values():
            _replace_invalid_citation_refs(item, allowed_refs, replacement)
    elif isinstance(value, list):
        for item in value:
            _replace_invalid_citation_refs(item, allowed_refs, replacement)


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
        write_text(out_path, body)
        write_json(manifest_path, manifest)
        outputs.append(_rel(out_path, cfg.book_dir))
        manifests.append(_rel(manifest_path, cfg.book_dir))

    return {"sources_md": outputs, "source_ref_manifests": manifests}


async def _normalize_with_layout_repair(
    parsed: dict[str, Any], source_id: str, cfg: BookConfig
) -> NormalizedSource:
    settings = _source_layout_repair_settings(cfg)
    block_overrides: dict[str, dict[str, Any]] = {}
    normalized = normalize_structured_source(
        raw_md=str(parsed.get("markdown") or ""),
        source_id=source_id,
        content_list_v2=parsed.get("content_list_v2"),
        content_list=parsed.get("content_list"),
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
    )
    vision_warnings: list[str] = []
    vision_overrides = await _vision_caption_overrides(normalized, cfg, vision_warnings)
    if vision_overrides:
        block_overrides.update(vision_overrides)
        normalized = normalize_structured_source(
            raw_md=str(parsed.get("markdown") or ""),
            source_id=source_id,
            content_list_v2=parsed.get("content_list_v2"),
            content_list=parsed.get("content_list"),
            block_overrides=block_overrides,
            min_confidence=settings["min_confidence"],
            max_candidates=settings["max_candidates"],
        )
    if settings["mode"] == "off" or not normalized.repair_candidates:
        if vision_warnings:
            normalized.manifest["vision_warnings"] = vision_warnings
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
        block_overrides=block_overrides,
        repair_patches=patches,
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
    )
    if vision_warnings:
        repaired.manifest["vision_warnings"] = vision_warnings
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
        for key in ("img_path", "image_path", "path", "url"):
            raw = value.get(key)
            if not isinstance(raw, str):
                continue
            rel_path = _asset_match(raw, path_index)
            if rel_path:
                value["asset_path"] = rel_path
                break
    for item in value.values():
        _attach_asset_paths(item, path_index)


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


async def _vision_caption_overrides(
    normalized: NormalizedSource, cfg: BookConfig, warnings: list[str]
) -> dict[str, dict[str, Any]]:
    settings = _vision_caption_settings(cfg)
    if settings["mode"] == "off":
        return {}
    candidates = _image_caption_candidates(normalized)[: settings["max_images"]]
    overrides: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        try:
            result = await run_with_cache(
                VisionCaptionAgent,
                candidate,
                model=cfg.model_for("vision"),
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            )
        except Exception as exc:  # noqa: BLE001 - captioning is best-effort enrichment
            warnings.append(
                f"vision caption failed for {candidate['block_id']}: {exc}"
            )
            continue
        overrides[candidate["block_id"]] = {
            "caption": result.result.caption_md,
            "asset_path": candidate["asset_path"],
        }
    return overrides


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
            if block.get("caption") or not block.get("asset_path"):
                continue
            candidates.append(
                {
                    "block_id": str(block.get("block_id") or ""),
                    "source_ref": str(block.get("page_ref") or page.get("source_ref") or ""),
                    "asset_path": str(block.get("asset_path") or ""),
                    "nearby_text": nearby_text,
                    "bbox": block.get("bbox"),
                }
            )
    return candidates


def _vision_caption_settings(cfg: BookConfig) -> dict[str, Any]:
    raw = cfg.generation.get("visionCaption")
    settings = raw if isinstance(raw, dict) else {}
    mode = str(settings.get("mode", "auto")).lower()
    if mode not in {"auto", "off"}:
        mode = "auto"
    return {
        "mode": mode,
        "max_images": _int_setting(settings.get("maxImagesPerSource"), 20),
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
    if not approved_path.exists():
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
    _clear_chapter_source_dirs(out_dir)
    chapter_sources: dict[str, str] = {}
    titles = split.result.chapter_titles or dict(_chapter_titles(approved_structure))
    for ch_id, md in split.result.chapters.items():
        title = titles.get(ch_id, ch_id)
        chapter_dir = ensure_dir(out_dir / ch_id)
        path = write_text(
            chapter_dir / "source.md",
            md if md.startswith("#") else f"# {ch_id} {title}\n\n{md.strip()}\n",
        )
        chapter_sources[ch_id] = _rel(path, cfg.book_dir)
    alignment_path = write_json(
        out_dir / "_alignment.json",
        {
            "alignment": split.result.alignment,
            "coverage": split.result.coverage,
            "chapter_titles": titles,
        },
    )
    report_path = write_text(
        cfg.work_dir / "logs" / "chapter-split-report.md", split.result.report_md
    )

    return {
        "chapter_sources": chapter_sources,
        "chapter_titles": titles,
        "chapter_alignment": _rel(alignment_path, cfg.book_dir),
        "chapter_split_report": _rel(report_path, cfg.book_dir),
        "cache_hit": split.cache_hit,
    }


def _clear_chapter_source_dirs(out_dir: Path) -> None:
    for child in out_dir.iterdir():
        if child.is_dir() and re.fullmatch(r"(ch\d+|chapter-\d+|appendix)", child.name):
            shutil.rmtree(child)


async def generate_node(state: State, cfg: BookConfig) -> State:
    if not state.get("chapter_sources"):
        msg = "generate requires chapter_sources; run split before generate"
        raise ValueError(msg)
    result_dir = ensure_dir(cfg.work_dir / "agent_results")
    chapter_results: dict[str, dict[str, str]] = {}
    cache_results: list[CacheResult] = []
    titles = state.get("chapter_titles", {})

    for ch_id, rel_source in state.get("chapter_sources", {}).items():
        source_path = cfg.book_dir / rel_source
        source_md = source_path.read_text(encoding="utf-8")
        title = _display_chapter_title(ch_id, str(titles.get(ch_id, ch_id)))
        payload = {
            "chapter_id": ch_id,
            "title": title,
            "source_md": source_md,
            "source_path": rel_source,
            "language": cfg.language,
            "book_notes": cfg.book_notes,
            "quiz_per_chapter": cfg.quiz_per_chapter,
            "cards_per_chapter": cfg.cards_per_chapter,
        }
        chapter_model = cfg.model_for("chapter")
        summary_model = cfg.model_for("summary")
        card_model = cfg.model_for("card")
        lesson_model = cfg.model_for("lesson")
        lesson = await run_with_cache(
            LessonAgent,
            payload,
            model=lesson_model,
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        chapter_result = lesson.result.chapter
        quiz_result = lesson.result.quiz
        card_result = lesson.result.card
        chapter_payload = {
            **payload,
            "chapter_result": _json_model(chapter_result),
            "chapter_body_md": chapter_result.body_md,
        }
        summary = await run_with_cache(
            SummaryAgent,
            chapter_payload,
            model=summary_model,
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        cache_results.extend([lesson, summary])
        paths = {
            "chapter": write_json(
                result_dir / f"{ch_id}.chapter.json",
                _agent_result_payload(LessonAgent, chapter_model, chapter_result),
            ),
            "summary": write_json(
                result_dir / f"{ch_id}.summary.json",
                _agent_result_payload(SummaryAgent, summary_model, summary.result),
            ),
            "quiz": write_json(
                result_dir / f"{ch_id}.quiz.json",
                _agent_result_payload(LessonAgent, lesson_model, quiz_result),
            ),
            "card": write_json(
                result_dir / f"{ch_id}.card.json",
                _agent_result_payload(LessonAgent, card_model, card_result),
            ),
        }
        chapter_results[ch_id] = {name: _rel(path, cfg.book_dir) for name, path in paths.items()}

    return {"agent_results": chapter_results, "cache_hit": _stage_cache_hit(cache_results)}


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    agent_results = {
        str(ch_id): dict(paths)
        for ch_id, paths in state.get("agent_results", {}).items()
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
        candidates.extend(
            item.model_dump(mode="json") for item in extract_result.result.concepts
        )
    result = await run_with_cache(
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
    cache_results.append(result)
    out_dir = ensure_dir(cfg.work_dir / "concepts")
    reconciled = write_json(out_dir / "reconciled.json", _json_model(result.result))
    write_json(
        cfg.work_dir / "agent_results" / "concepts.reconciled.json",
        _json_model(result.result),
    )
    alias_map = write_json(out_dir / "alias_map.json", result.result.alias_map)
    return {
        "reconciled_concepts": _rel(reconciled, cfg.book_dir),
        "alias_map": _rel(alias_map, cfg.book_dir),
        "agent_results": agent_results,
        "cache_hit": _stage_cache_hit(cache_results),
    }


async def concept_pages_node(state: State, cfg: BookConfig) -> State:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    _clear_generated_files(out_dir, "*.json")
    outputs: dict[str, str] = {}
    cache_results: list[CacheResult] = []
    used_stems: set[str] = set()
    for item in data.get("concepts", []):
        concept_input = {
            **item,
            "chapter_contexts": _concept_contexts(item, state, cfg),
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
        safe_name = _unique_file_stem(
            result.result.name, used_stems, fallback_prefix="concept"
        )
        path = write_json(out_dir / f"{safe_name}.json", _json_model(result.result))
        outputs[result.result.name] = _rel(path, cfg.book_dir)
    return {"concept_pages": outputs, "cache_hit": _stage_cache_hit(cache_results)}


def integrate_node(state: State, cfg: BookConfig) -> State:
    content_dir = ensure_dir(cfg.content_dir)
    chapters_dir = ensure_dir(content_dir / "chapters")
    concepts_dir = ensure_dir(content_dir / "concepts")
    _clear_generated_files(chapters_dir, "*.mdx")
    _clear_generated_files(concepts_dir, "*.mdx")
    chapter_outputs: list[str] = []
    concept_backlinks: dict[str, list[dict[str, str]]] = {}
    alias_map = _load_alias_map(state, cfg)
    concept_previews: dict[str, dict[str, str]] = {}
    for name, rel_path in state.get("concept_pages", {}).items():
        concept = read_json(cfg.book_dir / rel_path)
        concept_name = str(concept.get("name") or name)
        stem = Path(rel_path).stem
        preview = {
            "href": f"../concepts/{stem}",
            "title": concept_name,
            "summary": _preview_summary(
                str(concept.get("summary_md") or concept.get("body_md", ""))
            ),
        }
        concept_previews[str(name)] = preview
        concept_previews[concept_name] = preview

    for ch_id, paths in state.get("agent_results", {}).items():
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
        body_md = _normalize_chapter_body_heading(
            str(chapter["body_md"]), display_title
        )
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
                    "href": f"../chapters/{ch_id}",
                    "summary": str(summary["summary_md"]),
                }
            )
        path = write_text(
            chapters_dir / f"{ch_id}.mdx",
            (
                _frontmatter(
                    {
                        "chapter_id": ch_id,
                        "title": display_title,
                        "type": "chapter",
                        "summary": summary["summary_md"],
                        "concepts": concept_names,
                    }
                )
                + _insert_quiz_blocks(
                    normalize_mdx_math(
                        _normalize_concept_links(
                            body_md, alias_map, concept_previews
                        )
                    ),
                    quiz,
                )
                + "\n\n"
                + f"## Sources\n\n{citation_md}\n\n"
                + f"## Anki Cards\n\n{card_mdx}\n"
            ),
        )
        chapter_outputs.append(_rel(path, cfg.book_dir))

    for name, rel_path in state.get("concept_pages", {}).items():
        concept = read_json(cfg.book_dir / rel_path)
        safe_name = Path(rel_path).stem or _safe_file_stem(name, fallback_prefix="concept")
        backlinks = concept_backlinks.get(str(name)) or concept_backlinks.get(
            str(concept["name"]), []
        )
        backlink_md = "\n".join(
            "- "
            + _preview_link_mdx(
                item["href"], item["title"], item.get("summary", ""), item["title"]
            )
            for item in backlinks
        )
        referenced_by = f"\n\n## Referenced By\n\n{backlink_md}\n" if backlink_md else ""
        write_text(
            concepts_dir / f"{safe_name}.mdx",
            _frontmatter({"title": concept["name"], "type": "concept"})
            + f"# {concept['name']}\n\n"
            + normalize_mdx_math(str(concept["body_md"]))
            + referenced_by,
        )

    index_path = write_text(
        content_dir / "index.mdx",
        _frontmatter({"title": cfg.title})
        + f"# {cfg.title}\n\n"
        + "\n".join(
            f"- [chapters/{Path(path).stem}](/docs/chapters/{Path(path).stem})"
            for path in chapter_outputs
        )
        + "\n",
    )
    chapter_stems = [Path(path).stem for path in chapter_outputs]
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
            "pages": chapter_stems,
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
    return {"content_ready": True, "content_index": _rel(index_path, cfg.book_dir)}


def check_node(state: State, cfg: BookConfig) -> State:
    issues: list[Issue] = []
    if not (cfg.content_dir / "index.mdx").exists():
        issues.append(
            Issue(
                severity="error",
                code="MISSING_CONTENT_INDEX",
                message="content/docs/index.mdx was not generated",
                owner_task_id="content:index",
            )
        )
    for path in (cfg.content_dir / "chapters").glob("*.mdx"):
        text = path.read_text(encoding="utf-8")
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
    max_rounds = int(cfg.generation.get("maxRepairRounds", 1) or 1)
    rounds = dict(state.get("_repair_rounds", {}))
    out_dir = ensure_dir(cfg.work_dir / "repairs")
    outputs = []
    report = read_json(cfg.book_dir / state.get("check_report", "work/logs/check-report.json"))
    for target in targets:
        if int(rounds.get(target, 0)) >= max_rounds:
            continue
        rounds[target] = int(rounds.get(target, 0)) + 1
        target_issues = [
            issue for issue in report.get("issues", []) if issue.get("owner_task_id") == target
        ]
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
        _apply_repair(target, target_issues, state, cfg)
        path = write_json(out_dir / f"{target.replace(':', '-')}.json", _json_model(result.result))
        outputs.append(_rel(path, cfg.book_dir))
    return {"repairs": outputs, "repair_targets": [], "_repair_rounds": rounds}


def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    build_sqlite_index(cfg.content_dir, db_path)
    return {"sqlite": _rel(db_path, cfg.book_dir)}


NODE_FUNCTIONS = {
    "convert": convert_node,
    "structure": structure_node,
    "split": split_node,
    "generate": generate_node,
    "reconcile_concepts": reconcile_node,
    "concept_pages": concept_pages_node,
    "integrate": integrate_node,
    "check": check_node,
    "repair": repair_node,
    "index": index_node,
}
