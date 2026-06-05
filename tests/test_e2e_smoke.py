from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.site import materialize_site

ROOT = Path(__file__).resolve().parents[1]
APPROVED_STRUCTURE_MARKER = "# bookwiki: approved-structure"
PENDING_STRUCTURE_MARKER = "# bookwiki: pending-structure-review"


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


def approve_structure(book_dir: Path) -> None:
    approved = book_dir / "work" / "structure" / "approved-structure.yaml"
    text = approved.read_text(encoding="utf-8")
    if APPROVED_STRUCTURE_MARKER in [line.strip() for line in text.splitlines()]:
        return
    approved.write_text(
        text.replace(PENDING_STRUCTURE_MARKER, APPROVED_STRUCTURE_MARKER, 1),
        encoding="utf-8",
    )


@pytest.mark.smoke
def test_mini_book_pipeline_reaches_sqlite_and_serves_materialized_site(
    tmp_path: Path,
) -> None:
    book_dir = tmp_path / "books" / "mini"
    source = tmp_path / "notes.txt"
    source.write_text("BookWiki smoke source about state space search.", encoding="utf-8")

    run_script("scripts/init_book.py", str(book_dir), "--source", str(source))
    run_script("scripts/run.py", str(book_dir))
    approve_structure(book_dir)
    run_script("scripts/run.py", str(book_dir), "--resume")

    db_path = book_dir / "site" / ".bookwiki" / "bookwiki.sqlite"
    manifest_path = book_dir / "work" / "logs" / "run-manifest.json"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from documents").fetchone()[0] >= 1
        assert conn.execute("select count(*) from chunks").fetchone()[0] >= 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["outputs"]["sqlite"] == "site/.bookwiki/bookwiki.sqlite"

    site_dir = materialize_site(book_dir)
    response_text = serve_once(site_dir, "/content/docs/index.mdx")
    assert "title: Mini" in response_text


def serve_once(site_dir: Path, path: str) -> str:
    handler = partial(SimpleHTTPRequestHandler, directory=str(site_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}{path}", timeout=1
        ) as response:
            return response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
