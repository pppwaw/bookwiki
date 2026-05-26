from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bookwiki.convert.mineru_client import (
    MineruConversionError,
    convert_document_to_md,
    convert_pdf_to_md,
    normalize_mineru_markdown,
    normalize_mineru_parse_result,
)
from bookwiki.convert.source_normalizer import normalize_structured_source
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.pipeline.nodes import convert_node
from bookwiki.scheduler.config import default_config
from tests.fakes import RecordingRuntime


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


class _ZipMineruHandler(BaseHTTPRequestHandler):
    request_paths: list[str] = []
    status_polls = 0
    submitted_body = b""

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
            self._write_zip()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        self.__class__.request_paths.append(self.path)
        length = int(self.headers.get("content-length", "0"))
        self.__class__.submitted_body = self.rfile.read(length) if length else b""
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

    def _write_zip(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("tiny/tiny.md", "Combined markdown")
            archive.writestr(
                "tiny/tiny_content_list_v2.json",
                json.dumps(
                    [
                        {"page_idx": 0, "items": [{"type": "text", "content": "Zip page one."}]},
                        {"page_idx": 1, "items": [{"type": "text", "content": "Zip page two."}]},
                    ]
                ),
            )
        body = buffer.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
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


@pytest.fixture
def zip_mineru_api() -> str:
    _ZipMineruHandler.request_paths = []
    _ZipMineruHandler.status_polls = 0
    _ZipMineruHandler.submitted_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ZipMineruHandler)
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


def test_content_list_v2_pages_generate_page_refs_without_formfeed() -> None:
    normalized = normalize_structured_source(
        raw_md="Combined markdown without explicit page breaks",
        source_id="source",
        content_list_v2=[
            {
                "page_idx": 0,
                "items": [
                    {"type": "title", "content": "First Page"},
                    {"type": "text", "content": "First page body."},
                ],
            },
            {
                "page_idx": 1,
                "items": [
                    {"type": "title", "content": "Second Page"},
                    {"type": "text", "content": "Second page body."},
                ],
            },
        ],
    )

    assert "<!-- source_ref: source-p001 -->" in normalized.markdown
    assert "<!-- source_ref: source-p002 -->" in normalized.markdown
    assert "## Page 1" not in normalized.markdown
    assert "## Page 2" not in normalized.markdown
    assert "First page body." in normalized.markdown
    assert "Second page body." in normalized.markdown
    assert [page["source_ref"] for page in normalized.manifest["pages"]] == [
        "source-p001",
        "source-p002",
    ]


def test_content_list_v2_nested_content_dicts_render_text() -> None:
    normalized = normalize_structured_source(
        raw_md="Combined markdown without explicit page breaks",
        source_id="nested",
        content_list_v2=[
            [
                {
                    "type": "title",
                    "content": {"title_content": [{"text": "Chapter Title"}], "level": 1},
                },
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"text": "Nested paragraph "},
                            {"content": "body."},
                        ]
                    },
                },
                {
                    "type": "equation_interline",
                    "content": {"math_content": "x^2"},
                },
                {
                    "type": "table",
                    "content": {"html": "<table><tr><td>A</td></tr></table>"},
                },
            ]
        ],
    )

    assert "Chapter Title" in normalized.markdown
    assert "Nested paragraph body." in normalized.markdown
    assert "x^2" in normalized.markdown
    assert "<table><tr><td>A</td></tr></table>" in normalized.markdown
    assert len(normalized.manifest["pages"][0]["blocks"]) == 4


def test_legacy_content_list_pages_generate_page_refs() -> None:
    normalized = normalize_structured_source(
        raw_md="Combined markdown without explicit page breaks",
        source_id="legacy",
        content_list=[
            {"page_idx": 0, "type": "text", "text": "Legacy page one."},
            {"page_idx": 1, "type": "text", "text": "Legacy page two."},
        ],
    )

    assert "<!-- source_ref: legacy-p001 -->" in normalized.markdown
    assert "<!-- source_ref: legacy-p002 -->" in normalized.markdown
    assert "Legacy page one." in normalized.markdown
    assert "Legacy page two." in normalized.markdown


def test_source_layout_repair_link_table_parts_keeps_physical_pages() -> None:
    normalized = normalize_structured_source(
        raw_md="",
        source_id="tables",
        content_list_v2=[
            {
                "page_idx": 0,
                "items": [{"type": "table", "content": "| A |\n| 1 |", "bbox": [0, 0, 10, 10]}],
            },
            {
                "page_idx": 1,
                "items": [{"type": "table", "content": "| 2 |", "bbox": [0, 0, 10, 10]}],
            },
        ],
        repair_patches=[
            {
                "action": "link_table_parts",
                "source_block_id": "tables-p001-b001",
                "target_block_id": "tables-p002-b001",
                "confidence": 0.93,
                "reason": "same columns across adjacent pages",
            }
        ],
    )

    logical_table = normalized.manifest["logical_tables"][0]
    pages_by_block = {
        block["block_id"]: block["page_ref"]
        for page in normalized.manifest["pages"]
        for block in page["blocks"]
    }
    assert logical_table["canonical_ref"] == "tables-p001"
    assert [part["block_id"] for part in logical_table["parts"]] == [
        "tables-p001-b001",
        "tables-p002-b001",
    ]
    assert pages_by_block["tables-p001-b001"] == "tables-p001"
    assert pages_by_block["tables-p002-b001"] == "tables-p002"
    assert "logical_table: tables-table-001" in normalized.markdown


def test_source_layout_repair_rejects_unknown_block_refs() -> None:
    normalized = normalize_structured_source(
        raw_md="",
        source_id="badpatch",
        content_list_v2=[
            {
                "page_idx": 0,
                "items": [{"type": "table", "content": "| A |"}],
            }
        ],
        repair_patches=[
            {
                "action": "link_table_parts",
                "source_block_id": "badpatch-p001-b001",
                "target_block_id": "badpatch-p999-b001",
                "confidence": 0.93,
                "reason": "invalid target",
            }
        ],
    )

    assert normalized.manifest["logical_tables"] == []
    assert any(
        "unknown block id" in warning
        for warning in normalized.manifest["repair_warnings"]
    )


def test_no_low_confidence_candidates_for_plain_pages() -> None:
    normalized = normalize_structured_source(
        raw_md="",
        source_id="plain",
        content_list_v2=[
            {"page_idx": 0, "items": [{"type": "text", "content": "Only text."}]},
            {"page_idx": 1, "items": [{"type": "text", "content": "More text."}]},
        ],
    )

    assert normalized.repair_candidates == []


def test_normalize_mineru_parse_result_prefers_content_list_v2() -> None:
    md = normalize_mineru_parse_result(
        {
            "md_content": "Combined markdown",
            "content_list_v2": [
                {"page_idx": 0, "items": [{"type": "text", "content": "Page one."}]},
                {"page_idx": 1, "items": [{"type": "text", "content": "Page two."}]},
            ],
        },
        source_id="parsed",
    )

    assert "<!-- source_ref: parsed-p001 -->" in md
    assert "<!-- source_ref: parsed-p002 -->" in md
    assert "Combined markdown" not in md


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


def test_convert_document_to_md_extracts_content_list_v2_from_zip_result(
    tmp_path: Path, zip_mineru_api: str
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    md = convert_document_to_md(
        pdf,
        source_id="tiny",
        api_base_url=zip_mineru_api,
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert b'name="response_format_zip"' in _ZipMineruHandler.submitted_body
    assert b'name="return_content_list"' in _ZipMineruHandler.submitted_body
    assert "<!-- source_ref: tiny-p001 -->" in md
    assert "<!-- source_ref: tiny-p002 -->" in md
    assert "Zip page one." in md
    assert "Zip page two." in md


def test_convert_pptx_to_md_uses_async_mineru_tasks(
    tmp_path: Path, async_mineru_api: str
) -> None:
    deck = tmp_path / "tiny.pptx"
    _write_minimal_pptx(deck)

    md = convert_document_to_md(
        deck,
        source_id="tiny-deck",
        api_base_url=async_mineru_api,
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert "Async markdown text" in md
    assert "<!-- source_ref: tiny-deck-p001 -->" in md
    assert "/tasks" in _AsyncMineruHandler.request_paths
    assert "/tasks/task-1/result" in _AsyncMineruHandler.request_paths


def test_convert_text_to_md_wraps_one_file_source_ref(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("Line one\nLine two\n", encoding="utf-8")

    md = convert_text_to_md(notes, source_id="lecture-notes")

    assert md.startswith("# notes")
    assert "<!-- source_ref: lecture-notes-text -->" in md
    assert "Line one\nLine two" in md


def test_convert_node_routes_text_and_pptx_to_required_converters(
    tmp_path: Path, async_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "notes.txt").write_text("Plain notes", encoding="utf-8")
    _write_minimal_pptx(cfg.input_dir / "slides.pptx")
    monkeypatch.setenv("MINERU_API_URL", async_mineru_api)
    monkeypatch.setenv("MINERU_API_POLL_INTERVAL_SECONDS", "0.01")

    state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    outputs = sorted(state["sources_md"])
    assert outputs == [
        "work/sources_md/notes.md",
        "work/sources_md/slides.md",
    ]
    assert "Plain notes" in (cfg.book_dir / "work/sources_md/notes.md").read_text(
        encoding="utf-8"
    )
    slides_md = (cfg.book_dir / "work/sources_md/slides.md").read_text(
        encoding="utf-8"
    )
    assert "Async markdown text" in slides_md
    assert "Slide 1" not in slides_md
    assert "/tasks" in _AsyncMineruHandler.request_paths
    assert sorted(state["source_ref_manifests"]) == [
        "work/source_refs/notes.json",
        "work/source_refs/slides.json",
    ]


def test_convert_node_propagates_mineru_api_errors_for_pdf(tmp_path: Path, monkeypatch) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MINERU_API_TIMEOUT_SECONDS", "0.01")

    with pytest.raises(MineruConversionError, match="MinerU API is required"):
        asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))


def test_convert_node_calls_layout_repair_for_table_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    cfg.llm_runtime = RecordingRuntime(
        [
            {
                "patches": [
                    {
                        "action": "link_table_parts",
                        "source_block_id": "paper-p001-b001",
                        "target_block_id": "paper-p002-b001",
                        "confidence": 0.91,
                        "reason": "same table continues across adjacent pages",
                    }
                ],
                "notes": "linked table continuation",
            }
        ]
    )

    def fake_convert(path: Path, *, source_id: str):
        return {
            "markdown": "raw",
            "content_list_v2": [
                {"page_idx": 0, "items": [{"type": "table", "content": "| A |\n| 1 |"}]},
                {"page_idx": 1, "items": [{"type": "table", "content": "| 2 |"}]},
            ],
            "content_list": None,
        }

    monkeypatch.setattr("bookwiki.pipeline.nodes.convert_document_to_source", fake_convert)

    state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    manifest = json.loads(
        (cfg.book_dir / state["source_ref_manifests"][0]).read_text(encoding="utf-8")
    )
    assert cfg.llm_runtime.calls
    assert manifest["logical_tables"][0]["parts"][1]["block_id"] == "paper-p002-b001"


def test_convert_node_skips_layout_repair_without_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    cfg.llm_runtime = RecordingRuntime([])

    def fake_convert(path: Path, *, source_id: str):
        return {
            "markdown": "raw",
            "content_list_v2": [
                {"page_idx": 0, "items": [{"type": "text", "content": "Only text."}]}
            ],
            "content_list": None,
        }

    monkeypatch.setattr("bookwiki.pipeline.nodes.convert_document_to_source", fake_convert)

    asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    assert cfg.llm_runtime.calls == []
