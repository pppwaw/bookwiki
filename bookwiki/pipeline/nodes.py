from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from bookwiki.agents import (
    CardAgent,
    ChapterAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptReconcileAgent,
    QuizAgent,
    ReviewAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
)
from bookwiki.agents.prompting import prompt_version_for
from bookwiki.convert.common import source_id_from_stem
from bookwiki.convert.mineru_client import convert_document_to_md
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.split.chapter_splitter import parse_approved_structure
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_text

State = dict[str, Any]


def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _json_model(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)


def _agent_result_payload(agent_cls: type[Any], model: str, result: Any) -> dict[str, Any]:
    payload = _json_model(result)
    return {
        "_schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "_prompt_version": prompt_version_for(agent_cls.prompt_template),
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


def _quiz_items_for_mdx(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        rendered.append(
            {
                "id": str(item.get("id") or f"quiz-{index:03d}"),
                "question": str(item.get("question", "")),
                "choices": [str(choice) for choice in item.get("choices", [])],
                "answer": str(item.get("answer", "")),
                "explanation": str(item.get("explanation", "")),
                "citations": _citation_items(item.get("citations", [])),
            }
        )
    return rendered


def _card_items_for_mdx(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        rendered.append(
            {
                "id": str(item.get("id") or f"card-{index:03d}"),
                "front": str(item.get("front", "")),
                "back": str(item.get("back", "")),
                "citations": _citation_items(item.get("citations", [])),
            }
        )
    return rendered


def _frontmatter(data: dict[str, Any]) -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{body}\n---\n\n"


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


def convert_node(state: State, cfg: BookConfig) -> State:
    input_files = sorted(path for path in cfg.input_dir.iterdir() if path.is_file())
    if not input_files:
        msg = f"no input files found in {cfg.input_dir}"
        raise FileNotFoundError(msg)

    out_dir = ensure_dir(cfg.work_dir / "sources_md")
    outputs: list[str] = []
    for path in input_files:
        source_id = source_id_from_stem(path.stem)
        out_path = out_dir / f"{source_id}.md"
        suffix = path.suffix.lower()
        if suffix in {".pdf", ".pptx"}:
            body = convert_document_to_md(path, source_id=source_id)
        elif suffix in {".txt", ".md"}:
            body = convert_text_to_md(path, source_id=source_id)
        else:
            msg = f"unsupported source file type: {path.name}"
            raise ValueError(msg)
        write_text(out_path, body)
        outputs.append(_rel(out_path, cfg.book_dir))

    return {"sources_md": outputs}


async def structure_node(state: State, cfg: BookConfig) -> State:
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    results: list[CacheResult] = []
    summaries = []
    for path in source_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        result = await run_with_cache(
            SourceSummaryAgent,
            {"path": str(path), "sha256": sha256_text(text), "language": cfg.language},
            model=cfg.model_for("source_summary"),
            cache_dir=_cache_dir(cfg),
            runtime=cfg.llm_runtime,
        )
        results.append(result)
        summaries.append(_json_model(result.result))

    structure = await run_with_cache(
        StructureAgent,
        {"summaries": summaries, "strategy": "pedagogical", "language": cfg.language},
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
        write_text(approved_path, structure.result.proposed_structure_yaml)
    write_text(
        out_dir / "structure-review.md",
        "# Structure Review\n\n"
        "Review `proposed-structure.yaml`, edit `approved-structure.yaml`, then run split.\n\n"
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
        payload = {
            "chapter_id": ch_id,
            "title": titles.get(ch_id, ch_id),
            "source_md": source_md,
            "source_path": rel_source,
            "language": cfg.language,
            "quiz_per_chapter": cfg.quiz_per_chapter,
            "cards_per_chapter": cfg.cards_per_chapter,
        }
        chapter_model = cfg.model_for("chapter")
        summary_model = cfg.model_for("summary")
        quiz_model = cfg.model_for("quiz")
        card_model = cfg.model_for("card")
        chapter, summary, quiz, card = await asyncio.gather(
            run_with_cache(
                ChapterAgent,
                payload,
                model=chapter_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
            run_with_cache(
                SummaryAgent,
                payload,
                model=summary_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
            run_with_cache(
                QuizAgent,
                payload,
                model=quiz_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
            run_with_cache(
                CardAgent,
                payload,
                model=card_model,
                cache_dir=_cache_dir(cfg),
                runtime=cfg.llm_runtime,
            ),
        )
        cache_results.extend([chapter, summary, quiz, card])
        paths = {
            "chapter": write_json(
                result_dir / f"{ch_id}.chapter.json",
                _agent_result_payload(ChapterAgent, chapter_model, chapter.result),
            ),
            "summary": write_json(
                result_dir / f"{ch_id}.summary.json",
                _agent_result_payload(SummaryAgent, summary_model, summary.result),
            ),
            "quiz": write_json(
                result_dir / f"{ch_id}.quiz.json",
                _agent_result_payload(QuizAgent, quiz_model, quiz.result),
            ),
            "card": write_json(
                result_dir / f"{ch_id}.card.json",
                _agent_result_payload(CardAgent, card_model, card.result),
            ),
        }
        chapter_results[ch_id] = {name: _rel(path, cfg.book_dir) for name, path in paths.items()}

    return {"agent_results": chapter_results, "cache_hit": _stage_cache_hit(cache_results)}


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    for ch_id, paths in state.get("agent_results", {}).items():
        if "concepts" in paths:
            candidates.append(_agent_result(read_json(cfg.book_dir / paths["concepts"])))
            continue
        chapter = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        for concept_name in chapter.get("concepts", []):
            candidates.append(
                {
                    "name": str(concept_name),
                    "aliases": [],
                    "source_chapter_id": ch_id,
                    "owner_task_id": f"{ch_id}:concept_extract",
                }
            )
    result = await run_with_cache(
        ConceptReconcileAgent,
        candidates,
        model=cfg.model_for("concept"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    out_dir = ensure_dir(cfg.work_dir / "concepts")
    reconciled = write_json(out_dir / "reconciled.json", _json_model(result.result))
    alias_map = write_json(out_dir / "alias_map.json", result.result.alias_map)
    return {
        "reconciled_concepts": _rel(reconciled, cfg.book_dir),
        "alias_map": _rel(alias_map, cfg.book_dir),
        "cache_hit": result.cache_hit,
    }


async def concept_pages_node(state: State, cfg: BookConfig) -> State:
    data = read_json(cfg.book_dir / state["reconciled_concepts"], default={"concepts": []})
    out_dir = ensure_dir(cfg.work_dir / "agent_results" / "concepts")
    _clear_generated_files(out_dir, "*.json")
    outputs: dict[str, str] = {}
    cache_results: list[CacheResult] = []
    used_stems: set[str] = set()
    for item in data.get("concepts", []):
        result = await run_with_cache(
            ConceptAgent,
            item,
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

    for ch_id, paths in state.get("agent_results", {}).items():
        chapter = _agent_result(read_json(cfg.book_dir / paths["chapter"]))
        summary = _agent_result(read_json(cfg.book_dir / paths["summary"]))
        quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
        card = _agent_result(read_json(cfg.book_dir / paths["card"]))
        citations = chapter.get("citations", [])
        citation_md = "\n".join(f"- `{c['ref_id']}`: {c['quote']}" for c in citations)
        quiz_props = _mdx_prop(_quiz_items_for_mdx(quiz.get("items", [])))
        card_props = _mdx_prop(_card_items_for_mdx(card.get("items", [])))
        quiz_mdx = f"<QuizBlock items={{{quiz_props}}} />"
        card_mdx = f"<AnkiDeck cards={{{card_props}}} />"
        concept_names = [str(name) for name in chapter.get("concepts", [])]
        for name in concept_names:
            concept_backlinks.setdefault(name, []).append(
                {"title": str(chapter["title"]), "href": f"../chapters/{ch_id}"}
            )
        path = write_text(
            chapters_dir / f"{ch_id}.mdx",
            (
                _frontmatter(
                    {
                        "chapter_id": ch_id,
                        "title": chapter["title"],
                        "type": "chapter",
                        "summary": summary["summary_md"],
                        "concepts": concept_names,
                    }
                )
                + f"{chapter['body_md']}\n\n"
                + f"## Quiz\n\n{quiz_mdx}\n\n"
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
            f"- [{item['title']}]({item['href']})" for item in backlinks
        )
        referenced_by = f"\n\n## Referenced By\n\n{backlink_md}\n" if backlink_md else ""
        write_text(
            concepts_dir / f"{safe_name}.mdx",
            _frontmatter({"title": concept["name"], "type": "concept"})
            + f"# {concept['name']}\n\n{concept['body_md']}{referenced_by}",
        )

    index_path = write_text(
        content_dir / "index.mdx",
        f"# {cfg.title}\n\n"
        + "\n".join(
            f"- [chapters/{Path(path).stem}](./chapters/{Path(path).stem})"
            for path in chapter_outputs
        )
        + "\n",
    )
    write_json(
        content_dir / "meta.json",
        {
            "title": cfg.title,
            "pages": [Path(path).with_suffix("").as_posix() for path in chapter_outputs],
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
        if "## Sources" not in text:
            issues.append(
                Issue(
                    severity="error",
                    code="MISSING_SOURCES",
                    message=f"{path.name} has no Sources section",
                    owner_task_id=f"{path.stem}:chapter",
                )
            )
    status = "needs_repair" if issues else "passed"
    report = CheckReport(status=status, issues=issues)
    report_path = write_json(cfg.work_dir / "check-report.json", report.model_dump(mode="json"))
    return {
        "check_report": _rel(report_path, cfg.book_dir),
        "repair_targets": report.repair_targets,
    }


async def repair_node(state: State, cfg: BookConfig) -> State:
    targets = state.get("repair_targets", [])
    if not targets:
        return {"repair_targets": []}
    out_dir = ensure_dir(cfg.work_dir / "repairs")
    outputs = []
    for target in targets:
        result = await run_with_cache(
            ReviewAgent,
            {"owner_task_id": target},
            model=cfg.model_for("review"),
            cache_dir=_cache_dir(cfg),
            force=True,
            runtime=cfg.llm_runtime,
        )
        path = write_json(out_dir / f"{target.replace(':', '-')}.json", _json_model(result.result))
        outputs.append(_rel(path, cfg.book_dir))
    return {"repairs": outputs, "repair_targets": []}


def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    ensure_dir(db_path.parent)
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "create table documents "
            "(id integer primary key, path text unique, title text, body text)"
        )
        conn.execute("create table chunks (id integer primary key, document_id integer, body text)")
        conn.execute(
            "create table quiz_items "
            "(id integer primary key, chapter_id text, question text, answer text)"
        )
        conn.execute(
            "create table card_items "
            "(id integer primary key, chapter_id text, front text, back text)"
        )
        try:
            conn.execute(
                "create virtual table fts_chunks "
                "using fts5(body, content='chunks', content_rowid='id')"
            )
            has_fts = True
        except sqlite3.OperationalError:
            has_fts = False

        for path in sorted(cfg.content_dir.rglob("*.mdx")):
            body = path.read_text(encoding="utf-8")
            title = _document_title(body, path.stem)
            cur = conn.execute(
                "insert into documents(path, title, body) values (?, ?, ?)",
                (_rel(path, cfg.content_dir), title, body),
            )
            doc_id = int(cur.lastrowid)
            conn.execute(
                "insert into chunks(document_id, body) values (?, ?)", (doc_id, body[:2000])
            )

        for paths in state.get("agent_results", {}).values():
            quiz = _agent_result(read_json(cfg.book_dir / paths["quiz"]))
            card = _agent_result(read_json(cfg.book_dir / paths["card"]))
            for item in quiz.get("items", []):
                conn.execute(
                    "insert into quiz_items(chapter_id, question, answer) values (?, ?, ?)",
                    (quiz["chapter_id"], item["question"], item["answer"]),
                )
            for item in card.get("items", []):
                conn.execute(
                    "insert into card_items(chapter_id, front, back) values (?, ?, ?)",
                    (card["chapter_id"], item["front"], item["back"]),
                )
        if has_fts:
            conn.execute("insert into fts_chunks(fts_chunks) values ('rebuild')")
        conn.commit()

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
