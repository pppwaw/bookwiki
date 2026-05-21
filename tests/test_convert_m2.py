from __future__ import annotations

import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bookwiki.convert.mineru_client import (
    MineruConversionError,
    convert_pdf_to_md,
    normalize_mineru_markdown,
)
from bookwiki.convert.pptx_to_md import convert_pptx_to_md
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.pipeline.nodes import convert_node
from bookwiki.scheduler.config import default_config


class _AsyncMineruHandler(BaseHTTPRequestHandler):
    request_paths: list[str] = []
    status_polls = 0

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.__class__.request_paths.append(self.path)
        if self.path == "/health":
            self._write_json({"status": "healthy"})
            return
        if self.path == "/tasks/task-1":
            self.__class__.status_polls += 1
            status = "completed" if self.__class__.status_polls >= 2 else "processing"
            self._write_json({"task_id": "task-1", "status": status})
            return
        if self.path == "/tasks/task-1/result":
            self._write_json({"results": {"tiny": {"md_content": "Async markdown text"}}})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        self.__class__.request_paths.append(self.path)
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        if self.path == "/tasks":
            self._write_json({"task_id": "task-1", "status": "pending"})
            return
        self.send_error(404)

    def _write_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def async_mineru_api() -> str:
    _AsyncMineruHandler.request_paths = []
    _AsyncMineruHandler.status_polls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AsyncMineruHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_minimal_pptx(path: Path) -> None:
    slides = {
        "ppt/slides/slide1.xml": (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Lecture title</a:t></a:r>"
            "</a:p><a:p><a:r><a:t>First bullet</a:t></a:r></a:p></p:txBody></p:sp>"
            "</p:spTree></p:cSld></p:sld>"
        ),
        "ppt/slides/slide2.xml": (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Second slide</a:t></a:r>"
            "</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        ),
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        for name, body in slides.items():
            archive.writestr(name, body)


def test_normalize_mineru_markdown_adds_page_source_refs_and_cleans_breaks() -> None:
    md = normalize_mineru_markdown("First page\n\n\x0c\nSecond page", source_id="textbook")

    assert "<!-- source_ref: textbook-p001 -->" in md
    assert "<!-- source_ref: textbook-p002 -->" in md
    assert "\x0c" not in md
    assert "First page" in md
    assert "Second page" in md


def test_convert_pdf_to_md_requires_mineru_api(tmp_path: Path) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    with pytest.raises(MineruConversionError, match="MinerU API is required"):
        convert_pdf_to_md(
            pdf,
            source_id="textbook",
            api_base_url="http://127.0.0.1:1",
            timeout_seconds=0.01,
        )


def test_convert_pdf_to_md_uses_async_mineru_tasks(
    tmp_path: Path, async_mineru_api: str
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    md = convert_pdf_to_md(
        pdf,
        source_id="tiny",
        api_base_url=async_mineru_api,
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert "Async markdown text" in md
    assert "<!-- source_ref: tiny-p001 -->" in md
    assert "/tasks" in _AsyncMineruHandler.request_paths
    assert "/tasks/task-1/result" in _AsyncMineruHandler.request_paths
    assert "/file_parse" not in _AsyncMineruHandler.request_paths


def test_convert_text_to_md_wraps_one_file_source_ref(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("Line one\nLine two\n", encoding="utf-8")

    md = convert_text_to_md(notes, source_id="lecture-notes")

    assert md.startswith("# notes")
    assert "<!-- source_ref: lecture-notes-text -->" in md
    assert "Line one\nLine two" in md


def test_convert_pptx_to_md_extracts_slide_text_and_refs(tmp_path: Path) -> None:
    deck = tmp_path / "lecture.pptx"
    _write_minimal_pptx(deck)

    md = convert_pptx_to_md(deck, source_id="lecture9")

    assert "## Slide 1: Lecture title" in md
    assert "<!-- source_ref: lecture9-slide01 -->" in md
    assert "First bullet" in md
    assert "## Slide 2: Second slide" in md
    assert "<!-- source_ref: lecture9-slide02 -->" in md


def test_convert_node_routes_supported_non_pdf_inputs(tmp_path: Path) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "notes.txt").write_text("Plain notes", encoding="utf-8")
    _write_minimal_pptx(cfg.input_dir / "slides.pptx")

    state = convert_node({"book_id": cfg.book_id}, cfg)

    outputs = sorted(state["sources_md"])
    assert outputs == [
        "work/sources_md/notes.md",
        "work/sources_md/slides.md",
    ]
    assert "Plain notes" in (cfg.book_dir / "work/sources_md/notes.md").read_text(
        encoding="utf-8"
    )
    assert "Slide 1" in (cfg.book_dir / "work/sources_md/slides.md").read_text(
        encoding="utf-8"
    )


def test_convert_node_propagates_mineru_api_errors_for_pdf(tmp_path: Path, monkeypatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MINERU_API_TIMEOUT_SECONDS", "0.01")

    with pytest.raises(MineruConversionError, match="MinerU API is required"):
        convert_node({"book_id": cfg.book_id}, cfg)
