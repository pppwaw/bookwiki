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
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_init_book_and_run_stub_pipeline_to_sqlite(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki offline CLI smoke source.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    run_script("scripts/run.py", str(book_dir))

    db_path = book_dir / "site" / ".bookwiki" / "bookwiki.sqlite"
    manifest_path = book_dir / "work" / "logs" / "run-manifest.json"
    vault_index = book_dir / "vault" / "index.md"

    assert db_path.exists()
    assert vault_index.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["book_id"] == "mini"
    assert manifest["status"] == "completed"
    assert manifest["nodes"][-1]["name"] == "index"

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


def test_dry_run_prints_mermaid_and_estimate(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki dry run smoke source.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    result = run_script("scripts/run.py", str(book_dir), "--dry-run")

    assert "graph TD" in result.stdout
    assert "Estimated tokens" in result.stdout
    assert not (book_dir / "site" / ".bookwiki" / "bookwiki.sqlite").exists()
