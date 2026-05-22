from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_script(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["BOOKWIKI_TEST_LLM"] = "1"
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_init_book_and_run_fake_llm_pipeline_to_sqlite(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki offline CLI smoke source.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    run_script("scripts/run.py", str(book_dir))

    db_path = book_dir / "site" / ".bookwiki" / "bookwiki.sqlite"
    manifest_path = book_dir / "work" / "logs" / "run-manifest.json"
    content_index = book_dir / "content" / "docs" / "index.mdx"

    assert db_path.exists()
    assert content_index.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["book_id"] == "mini"
    assert manifest["status"] == "completed"
    assert manifest["nodes"][-1]["name"] == "index"

    chapter_results = sorted((book_dir / "work" / "agent_results").glob("*.chapter.json"))
    assert chapter_results
    chapter_payload = json.loads(chapter_results[0].read_text(encoding="utf-8"))
    assert chapter_payload["_schema_version"] == "llm.v1"
    assert chapter_payload["_prompt_version"].startswith("v")
    assert chapter_payload["_agent"] == "ChapterAgent"
    assert chapter_payload["result"]["owner_task_id"].endswith(":chapter")

    with sqlite3.connect(db_path) as conn:
        docs = conn.execute("select count(*) from documents").fetchone()[0]
        chunks = conn.execute("select count(*) from chunks").fetchone()[0]

    assert docs >= 1
    assert chunks >= 1


def test_resume_reports_cache_hits_after_completed_run(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki resume smoke source.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    run_script("scripts/run.py", str(book_dir))
    resumed = run_script("scripts/run.py", str(book_dir), "--resume")

    assert "resume: completed checkpoint found" in resumed.stdout
    assert "cache_hit" in resumed.stdout


def test_completed_checkpoint_is_not_reused_after_language_change(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki language checkpoint smoke source.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    run_script("scripts/run.py", str(book_dir))
    (book_dir / "input" / source.name).unlink()
    config_path = book_dir / "book.config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["language"] = "en-US"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_script("scripts/structure.py", str(book_dir))

    assert "completed checkpoint found" not in result.stdout
    assert "stage complete: structure" in result.stdout
    checkpoint = json.loads(
        (book_dir / "work" / ".cache" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "paused"
    assert checkpoint["next_node"] == "split"
    assert checkpoint["config_hash"]


def test_dry_run_prints_mermaid_and_estimate(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki dry run smoke source.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    result = run_script("scripts/run.py", str(book_dir), "--dry-run")

    assert "graph TD" in result.stdout
    assert "Estimated tokens" in result.stdout
    assert not (book_dir / "site" / ".bookwiki" / "bookwiki.sqlite").exists()


def test_structure_then_split_allows_manual_approved_structure_edit(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    intro = tmp_path / "intro.txt"
    advanced = tmp_path / "advanced.txt"
    intro.write_text("State space search and goals.", encoding="utf-8")
    advanced.write_text("Heuristic search and A star.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(intro))
    input_dir = book_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / advanced.name).write_text(advanced.read_text(encoding="utf-8"), encoding="utf-8")

    run_script("scripts/convert.py", str(book_dir))
    run_script("scripts/structure.py", str(book_dir))

    checkpoint = json.loads(
        (book_dir / "work" / ".cache" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "paused"
    assert checkpoint["next_node"] == "split"

    approved = book_dir / "work" / "structure" / "approved-structure.yaml"
    approved.write_text(
        "chapters:\n"
        "  - title: Chapter 1 Intro Search\n"
        "    topics:\n"
        "      - State space search\n"
        "    source_refs:\n"
        "      - intro-text\n"
        "  - title: Chapter 2 Heuristic Search\n"
        "    topics:\n"
        "      - Heuristic search\n"
        "    source_refs:\n"
        "      - advanced-text\n",
        encoding="utf-8",
    )

    run_script("scripts/split.py", str(book_dir))

    ch01 = book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md"
    ch02 = book_dir / "work" / "chapter_sources" / "chapter-2" / "source.md"
    assert "State space search" in ch01.read_text(encoding="utf-8")
    assert "A star" in ch02.read_text(encoding="utf-8")
