from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

from bookwiki.agents import (
    CardAgent,
    ChapterAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptExtractAgent,
    ConceptReconcileAgent,
    QuizAgent,
    ReviewAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
)
from bookwiki.convert.common import source_id_from_stem
from bookwiki.convert.mineru_client import convert_pdf_to_md
from bookwiki.convert.pptx_to_md import convert_pptx_to_md
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text

State = dict[str, Any]


def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _json_model(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)


def _read_all_markdown(paths: list[Path]) -> str:
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)


def _chapter_titles(approved_md: str) -> list[tuple[str, str]]:
    found = re.findall(r"^##\s+(ch\d+)\s+(.+)$", approved_md, flags=re.MULTILINE)
    return found or [("ch01", "Foundations"), ("ch02", "Practice")]


def _cache_dir(cfg: BookConfig) -> Path:
    return cfg.cache_dir / "tasks"


def _stage_cache_hit(results: list[CacheResult]) -> bool:
    return bool(results) and all(item.cache_hit for item in results)


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
        if suffix == ".pdf":
            body = convert_pdf_to_md(path, source_id=source_id)
        elif suffix == ".pptx":
            body = convert_pptx_to_md(path, source_id=source_id)
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
        result = await run_with_cache(
            SourceSummaryAgent,
            path,
            model=cfg.model_for("source_summary"),
            cache_dir=_cache_dir(cfg),
        )
        results.append(result)
        summaries.append(_json_model(result.result))

    structure = await run_with_cache(
        StructureAgent,
        summaries,
        model=cfg.model_for("structure"),
        cache_dir=_cache_dir(cfg),
    )
    results.append(structure)

    out_dir = ensure_dir(cfg.work_dir / "structure")
    proposed_path = write_text(
        out_dir / "proposed-structure.md", structure.result.proposed_structure_md
    )
    approved_path = out_dir / "approved-structure.md"
    if not approved_path.exists():
        write_text(approved_path, structure.result.proposed_structure_md)
    write_text(
        out_dir / "structure-review.md",
        "# Structure Review\n\nM1 stub auto-created approved-structure.md for end-to-end runs.\n",
    )

    return {
        "proposed_structure": _rel(proposed_path, cfg.book_dir),
        "approved_structure": _rel(approved_path, cfg.book_dir),
        "cache_hit": _stage_cache_hit(results),
    }


async def split_node(state: State, cfg: BookConfig) -> State:
    approved_path = cfg.book_dir / state.get(
        "approved_structure", "work/structure/approved-structure.md"
    )
    approved_md = approved_path.read_text(encoding="utf-8")
    source_paths = [cfg.book_dir / rel for rel in state.get("sources_md", [])]
    source_md = _read_all_markdown(source_paths)
    split = await run_with_cache(
        ChapterSplitAgent,
        {"source_md": source_md, "approved_structure": approved_md},
        model=cfg.model_for("split"),
        cache_dir=_cache_dir(cfg),
    )

    out_dir = ensure_dir(cfg.work_dir / "chapter_sources")
    chapter_sources: dict[str, str] = {}
    titles = dict(_chapter_titles(approved_md))
    for ch_id, md in split.result.chapters.items():
        title = titles.get(ch_id, ch_id)
        chapter_dir = ensure_dir(out_dir / ch_id)
        path = write_text(
            chapter_dir / "source.md",
            f"# {ch_id} {title}\n\n{md.strip()}\n\n<!-- source_ref: Prob_GZIC-p001 -->\n",
        )
        chapter_sources[ch_id] = _rel(path, cfg.book_dir)
    report_path = write_text(
        cfg.work_dir / "logs" / "chapter-split-report.md", split.result.report_md
    )

    return {
        "chapter_sources": chapter_sources,
        "chapter_titles": titles,
        "chapter_split_report": _rel(report_path, cfg.book_dir),
        "cache_hit": split.cache_hit,
    }


async def generate_node(state: State, cfg: BookConfig) -> State:
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
        }
        chapter = await run_with_cache(
            ChapterAgent,
            payload,
            model=cfg.model_for("chapter"),
            cache_dir=_cache_dir(cfg),
        )
        summary, quiz, card, concept = await asyncio.gather(
            run_with_cache(
                SummaryAgent, payload, model=cfg.model_for("summary"), cache_dir=_cache_dir(cfg)
            ),
            run_with_cache(
                QuizAgent, payload, model=cfg.model_for("quiz"), cache_dir=_cache_dir(cfg)
            ),
            run_with_cache(
                CardAgent, payload, model=cfg.model_for("card"), cache_dir=_cache_dir(cfg)
            ),
            run_with_cache(
                ConceptExtractAgent,
                payload,
                model=cfg.model_for("concept"),
                cache_dir=_cache_dir(cfg),
            ),
        )
        cache_results.extend([chapter, summary, quiz, card, concept])
        paths = {
            "chapter": write_json(
                result_dir / f"{ch_id}.chapter.json", _json_model(chapter.result)
            ),
            "summary": write_json(
                result_dir / f"{ch_id}.summary.json", _json_model(summary.result)
            ),
            "quiz": write_json(result_dir / f"{ch_id}.quiz.json", _json_model(quiz.result)),
            "card": write_json(result_dir / f"{ch_id}.card.json", _json_model(card.result)),
            "concepts": write_json(
                result_dir / f"{ch_id}.concepts.json", _json_model(concept.result)
            ),
        }
        chapter_results[ch_id] = {name: _rel(path, cfg.book_dir) for name, path in paths.items()}

    return {"agent_results": chapter_results, "cache_hit": _stage_cache_hit(cache_results)}


async def reconcile_node(state: State, cfg: BookConfig) -> State:
    candidates = []
    for paths in state.get("agent_results", {}).values():
        candidates.append(read_json(cfg.book_dir / paths["concepts"]))
    result = await run_with_cache(
        ConceptReconcileAgent,
        candidates,
        model=cfg.model_for("concept"),
        cache_dir=_cache_dir(cfg),
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
    outputs: dict[str, str] = {}
    cache_results: list[CacheResult] = []
    for item in data.get("concepts", []):
        result = await run_with_cache(
            ConceptAgent,
            item,
            model=cfg.model_for("concept"),
            cache_dir=_cache_dir(cfg),
        )
        cache_results.append(result)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", result.result.name).strip("-") or "concept"
        path = write_json(out_dir / f"{safe_name}.json", _json_model(result.result))
        outputs[result.result.name] = _rel(path, cfg.book_dir)
    return {"concept_pages": outputs, "cache_hit": _stage_cache_hit(cache_results)}


def integrate_node(state: State, cfg: BookConfig) -> State:
    chapters_dir = ensure_dir(cfg.vault_dir / "chapters")
    concepts_dir = ensure_dir(cfg.vault_dir / "concepts")
    chapter_outputs: list[str] = []

    for ch_id, paths in state.get("agent_results", {}).items():
        chapter = read_json(cfg.book_dir / paths["chapter"])
        summary = read_json(cfg.book_dir / paths["summary"])
        quiz = read_json(cfg.book_dir / paths["quiz"])
        card = read_json(cfg.book_dir / paths["card"])
        citations = chapter.get("citations", [])
        citation_md = "\n".join(f"- `{c['ref_id']}`: {c['quote']}" for c in citations)
        quiz_md = "\n".join(
            f"- {item['question']} Answer: {item['answer']}" for item in quiz.get("items", [])
        )
        card_md = "\n".join(
            f"- **{item['front']}**: {item['back']}" for item in card.get("items", [])
        )
        concept_links = " ".join(f"[[{name}]]" for name in chapter.get("concepts", []))
        path = write_text(
            chapters_dir / f"{ch_id}.md",
            (
                f"---\nchapter_id: {ch_id}\ntitle: {chapter['title']}\n---\n\n"
                f"{chapter['body_md']}\n\n"
                f"## Summary\n\n{summary['summary_md']}\n\n"
                f"## Concepts\n\n{concept_links}\n\n"
                f"## Quiz\n\n{quiz_md}\n\n"
                f"## Cards\n\n{card_md}\n\n"
                f"## Sources\n\n{citation_md}\n"
            ),
        )
        chapter_outputs.append(_rel(path, cfg.book_dir))

    for name, rel_path in state.get("concept_pages", {}).items():
        concept = read_json(cfg.book_dir / rel_path)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "concept"
        write_text(
            concepts_dir / f"{safe_name}.md", f"# {concept['name']}\n\n{concept['body_md']}\n"
        )

    index_path = write_text(
        cfg.vault_dir / "index.md",
        f"# {cfg.title}\n\n"
        + "\n".join(f"- [[chapters/{Path(path).stem}]]" for path in chapter_outputs)
        + "\n",
    )
    return {"vault_ready": True, "vault_index": _rel(index_path, cfg.book_dir)}


def check_node(state: State, cfg: BookConfig) -> State:
    issues: list[Issue] = []
    if not (cfg.vault_dir / "index.md").exists():
        issues.append(
            Issue(
                severity="error",
                code="MISSING_VAULT_INDEX",
                message="vault/index.md was not generated",
                owner_task_id="vault:index",
            )
        )
    for path in (cfg.vault_dir / "chapters").glob("*.md"):
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

        for path in sorted(cfg.vault_dir.rglob("*.md")):
            body = path.read_text(encoding="utf-8")
            title = body.splitlines()[0].lstrip("# ").strip() if body.splitlines() else path.stem
            cur = conn.execute(
                "insert into documents(path, title, body) values (?, ?, ?)",
                (_rel(path, cfg.vault_dir), title, body),
            )
            doc_id = int(cur.lastrowid)
            conn.execute(
                "insert into chunks(document_id, body) values (?, ?)", (doc_id, body[:2000])
            )

        for paths in state.get("agent_results", {}).values():
            quiz = read_json(cfg.book_dir / paths["quiz"])
            card = read_json(cfg.book_dir / paths["card"])
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
