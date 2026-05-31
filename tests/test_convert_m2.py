from __future__ import annotations

import asyncio
import hashlib
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
    convert_document_to_source,
    convert_pdf_to_md,
    normalize_mineru_markdown,
    normalize_mineru_parse_result,
)
from bookwiki.convert.source_normalizer import normalize_structured_source
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.pipeline.nodes import convert_node
from bookwiki.scheduler.config import default_config
from tests.fakes import RecordingRuntime


@pytest.fixture(autouse=True)
def isolated_mineru_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOKWIKI_DOTENV_PATH", "C:/definitely/missing/bookwiki.env")
    for name in (
        "MINERU_BACKEND",
        "MINERU_CLOUD_API_URL",
        "MINERU_API_URL",
        "MINERU_API_TOKEN",
        "MINERU_TOKEN",
        "MINERU_MODEL_VERSION",
        "MINERU_API_TIMEOUT_SECONDS",
        "MINERU_API_POLL_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


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
            archive.writestr("tiny/images/figure-1.png", b"\x89PNG\r\n\x1a\nfigure")
            archive.writestr(
                "tiny/tiny_content_list_v2.json",
                json.dumps(
                    [
                        {
                            "page_idx": 0,
                            "items": [
                                {"type": "text", "content": "Zip page one."},
                                {
                                    "type": "image",
                                    "img_path": "images/figure-1.png",
                                    "bbox": [1, 2, 30, 40],
                                },
                            ],
                        },
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


class _CloudV4MineruHandler(BaseHTTPRequestHandler):
    request_paths: list[str] = []
    status_polls = 0
    submitted_payload: dict[str, object] = {}
    auth_header = ""
    uploaded_body = b""
    upload_content_type: str | None = None

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        self.__class__.request_paths.append(self.path)
        self.__class__.auth_header = self.headers.get("Authorization", "")
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        if self.path == "/api/v4/file-urls/batch":
            self.__class__.submitted_payload = json.loads(body.decode("utf-8"))
            upload_url = f"http://127.0.0.1:{self.server.server_port}/upload/tiny.pdf"
            self._write_json(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "batch-1",
                        "file_urls": [
                            {
                                "file_name": "tiny.pdf",
                                "upload_url": upload_url,
                            }
                        ],
                    },
                    "msg": "ok",
                }
            )
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        self.__class__.request_paths.append(self.path)
        self.__class__.upload_content_type = self.headers.get("Content-Type")
        length = int(self.headers.get("content-length", "0"))
        self.__class__.uploaded_body = self.rfile.read(length) if length else b""
        if self.path == "/upload/tiny.pdf":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self) -> None:
        self.__class__.request_paths.append(self.path)
        if self.path == "/api/v4/extract-results/batch/batch-1":
            self.__class__.status_polls += 1
            state = "done" if self.__class__.status_polls >= 2 else "running"
            result: dict[str, object] = {
                "file_name": "tiny.pdf",
                "state": state,
                "err_msg": "",
            }
            if state == "done":
                result["full_zip_url"] = (
                    f"http://127.0.0.1:{self.server.server_port}/result/tiny.zip"
                )
            self._write_json(
                {
                    "code": 0,
                    "data": {"batch_id": "batch-1", "extract_result": [result]},
                    "msg": "ok",
                }
            )
            return
        if self.path == "/result/tiny.zip":
            self._write_zip()
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
            archive.writestr("tiny/full.md", "Cloud combined markdown")
            archive.writestr("tiny/images/figure-1.png", b"\x89PNG\r\n\x1a\ncloud")
            archive.writestr(
                "tiny/tiny_content_list_v2.json",
                json.dumps(
                    [
                        {
                            "page_idx": 0,
                            "items": [
                                {"type": "text", "content": "Cloud page one."},
                                {
                                    "type": "image",
                                    "img_path": "images/figure-1.png",
                                    "bbox": [4, 5, 60, 70],
                                },
                            ],
                        },
                        {"page_idx": 1, "items": [{"type": "text", "content": "Cloud page two."}]},
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


@pytest.fixture
def cloud_v4_mineru_api() -> str:
    _CloudV4MineruHandler.request_paths = []
    _CloudV4MineruHandler.status_polls = 0
    _CloudV4MineruHandler.submitted_payload = {}
    _CloudV4MineruHandler.auth_header = ""
    _CloudV4MineruHandler.uploaded_body = b""
    _CloudV4MineruHandler.upload_content_type = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CloudV4MineruHandler)
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
                            {"type": "inline", "content": " Plain inline text."},
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
    assert "Nested paragraph body. Plain inline text." in normalized.markdown
    assert "$Plain inline text.$" not in normalized.markdown
    assert "$$\nx^2\n$$" in normalized.markdown
    assert "<table><tr><td>A</td></tr></table>" in normalized.markdown
    assert len(normalized.manifest["pages"][0]["blocks"]) == 4


def test_content_list_math_fields_render_markdown_math_once() -> None:
    normalized = normalize_structured_source(
        raw_md="",
        source_id="math",
        content_list=[
            {"page_idx": 0, "type": "equation", "text": "$$\ny = mx + b\n$$"},
        ],
        content_list_v2=[],
    )

    assert normalized.markdown.count("$$\ny = mx + b\n$$") == 1
    assert "$$$" not in normalized.markdown


def test_content_list_v2_math_content_and_spans_render_markdown_math() -> None:
    normalized = normalize_structured_source(
        raw_md="",
        source_id="mathv2",
        content_list_v2=[
            [
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "Let "},
                            {"type": "inline_equation", "content": "$x$"},
                            {"type": "text", "content": " be observed."},
                        ]
                    },
                },
                {
                    "type": "equation_interline",
                    "content": {"math_content": "$$\ny=x^2\n$$", "math_type": "interline"},
                },
                {
                    "type": "text",
                    "lines": [
                        {
                            "spans": [
                                {"type": "text", "content": "Then "},
                                {"type": "inline_equation", "content": "E[X]"},
                                {"type": "text", "content": " follows."},
                            ]
                        },
                        {
                            "spans": [
                                {"type": "interline_equation", "content": "V(X)=1"},
                            ]
                        },
                    ],
                },
            ]
        ],
    )

    assert "Let $x$ be observed." in normalized.markdown
    assert normalized.markdown.count("$$\ny=x^2\n$$") == 1
    assert "Then $E[X]$ follows." in normalized.markdown
    assert "$$\nV(X)=1\n$$" in normalized.markdown


def test_image_blocks_render_book_figure_with_asset_metadata() -> None:
    normalized = normalize_structured_source(
        raw_md="",
        source_id="figures",
        content_list_v2=[
            {
                "page_idx": 0,
                "items": [
                    {
                        "type": "image",
                        "asset_path": "work/assets/figures/figure-1.png",
                        "caption": "Sampling distribution diagram.",
                        "bbox": [1, 2, 30, 40],
                    }
                ],
            }
        ],
    )

    block = normalized.manifest["pages"][0]["blocks"][0]
    assert block["type"] == "image"
    assert block["asset_path"] == "work/assets/figures/figure-1.png"
    assert block["caption"] == "Sampling distribution diagram."
    assert block["bbox"] == [1, 2, 30, 40]
    assert '<BookFigure id="figures-p001-b001"' in normalized.markdown
    assert 'src="/bookwiki-assets/figures/figure-1.png"' in normalized.markdown
    assert 'sourceRef="figures-p001"' in normalized.markdown
    assert 'caption="Sampling distribution diagram."' in normalized.markdown


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


def test_cloud_v4_requires_mineru_api_token(
    tmp_path: Path, cloud_v4_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_TOKEN", raising=False)

    with pytest.raises(MineruConversionError, match="MINERU_API_TOKEN"):
        convert_document_to_md(
            pdf,
            source_id="tiny",
            api_base_url=cloud_v4_mineru_api,
            backend="cloud-v4",
            timeout_seconds=5,
            poll_interval_seconds=0.01,
        )

    assert _CloudV4MineruHandler.request_paths == []


def test_convert_document_to_md_uses_mineru_cloud_v4_upload_flow(
    tmp_path: Path, cloud_v4_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf_bytes = b"%PDF-1.4\n%%EOF\n"
    pdf.write_bytes(pdf_bytes)
    monkeypatch.setenv("MINERU_API_TOKEN", "cloud-token")

    md = convert_document_to_md(
        pdf,
        source_id="tiny",
        api_base_url=cloud_v4_mineru_api,
        backend="cloud-v4",
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert "<!-- source_ref: tiny-p001 -->" in md
    assert "<!-- source_ref: tiny-p002 -->" in md
    assert "Cloud page one." in md
    assert "Cloud page two." in md
    assert _CloudV4MineruHandler.auth_header == "Bearer cloud-token"
    assert _CloudV4MineruHandler.uploaded_body == pdf_bytes
    assert _CloudV4MineruHandler.upload_content_type is None
    assert _CloudV4MineruHandler.submitted_payload == {
        "files": [{"name": "tiny.pdf", "data_id": "tiny"}],
        "model_version": "vlm",
    }
    assert "/api/v4/file-urls/batch" in _CloudV4MineruHandler.request_paths
    assert "/upload/tiny.pdf" in _CloudV4MineruHandler.request_paths
    assert "/api/v4/extract-results/batch/batch-1" in _CloudV4MineruHandler.request_paths
    assert "/result/tiny.zip" in _CloudV4MineruHandler.request_paths
    assert "/health" not in _CloudV4MineruHandler.request_paths
    assert "/tasks" not in _CloudV4MineruHandler.request_paths


def test_cloud_v4_uses_cloud_url_instead_of_local_mineru_api_url(
    tmp_path: Path, cloud_v4_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setenv("MINERU_BACKEND", "cloud-v4")
    monkeypatch.setenv("MINERU_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MINERU_CLOUD_API_URL", cloud_v4_mineru_api)
    monkeypatch.setenv("MINERU_API_TOKEN", "cloud-token")

    md = convert_document_to_md(
        pdf,
        source_id="tiny",
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert "Cloud page one." in md
    assert "/api/v4/file-urls/batch" in _CloudV4MineruHandler.request_paths


def test_convert_document_loads_mineru_settings_from_dotenv(
    tmp_path: Path, cloud_v4_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "MINERU_BACKEND=cloud-v4",
                f"MINERU_CLOUD_API_URL={cloud_v4_mineru_api}",
                "MINERU_API_TOKEN=cloud-token",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "MINERU_BACKEND",
        "MINERU_CLOUD_API_URL",
        "MINERU_API_URL",
        "MINERU_API_TOKEN",
        "MINERU_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOKWIKI_DOTENV_PATH", str(env_path))

    md = convert_document_to_md(
        pdf,
        source_id="tiny",
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert "Cloud page one." in md
    assert "/api/v4/file-urls/batch" in _CloudV4MineruHandler.request_paths


def test_convert_document_to_source_preserves_cloud_v4_assets(
    tmp_path: Path, cloud_v4_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setenv("MINERU_API_TOKEN", "cloud-token")

    parsed = convert_document_to_source(
        pdf,
        source_id="tiny",
        api_base_url=cloud_v4_mineru_api,
        backend="cloud-v4",
        timeout_seconds=5,
        poll_interval_seconds=0.01,
    )

    assert parsed["source_id"] == "tiny"
    assert parsed["assets"][0]["filename"] == "figure-1.png"
    assert parsed["assets"][0]["data"] == b"\x89PNG\r\n\x1a\ncloud"


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


def test_convert_node_reuses_matching_hashed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    pdf = cfg.input_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    calls = 0

    def fake_convert(path: Path, *, source_id: str):
        nonlocal calls
        calls += 1
        return {
            "markdown": "raw",
            "content_list_v2": [
                {"page_idx": 0, "items": [{"type": "text", "content": "Cached text."}]}
            ],
            "content_list": None,
            "assets": [],
        }

    monkeypatch.setattr("bookwiki.pipeline.nodes.convert_document_to_source", fake_convert)

    first_state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    manifest_path = cfg.book_dir / first_state["source_ref_manifests"][0]
    markdown_path = cfg.book_dir / first_state["sources_md"][0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_file"]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert manifest["outputs"]["markdown_sha256"] == hashlib.sha256(
        markdown_path.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()

    def fail_convert(path: Path, *, source_id: str):
        pytest.fail("matching convert artifact should be reused")

    monkeypatch.setattr("bookwiki.pipeline.nodes.convert_document_to_source", fail_convert)

    second_state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    assert calls == 1
    assert second_state == first_state


def test_convert_node_writes_mineru_zip_image_assets_and_manifest(
    tmp_path: Path, zip_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    pdf = cfg.input_dir / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setenv("MINERU_API_URL", zip_mineru_api)
    monkeypatch.setenv("MINERU_API_POLL_INTERVAL_SECONDS", "0.01")
    cfg.generation["visionCaption"] = {"mode": "off"}

    state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    asset_path = cfg.work_dir / "assets" / "tiny" / "figure-1.png"
    assert asset_path.read_bytes() == b"\x89PNG\r\n\x1a\nfigure"
    source_md = (cfg.book_dir / state["sources_md"][0]).read_text(encoding="utf-8")
    assert '<BookFigure id="tiny-p001-b002"' in source_md
    assert 'src="/bookwiki-assets/tiny/figure-1.png"' in source_md
    manifest = json.loads(
        (cfg.book_dir / state["source_ref_manifests"][0]).read_text(encoding="utf-8")
    )
    image_block = manifest["pages"][0]["blocks"][1]
    assert image_block["type"] == "image"
    assert image_block["asset_path"] == "work/assets/tiny/figure-1.png"
    assert image_block["bbox"] == [1, 2, 30, 40]


def test_convert_node_uses_mineru_cloud_v4_backend(
    tmp_path: Path, cloud_v4_mineru_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    pdf = cfg.input_dir / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setenv("MINERU_BACKEND", "cloud-v4")
    monkeypatch.setenv("MINERU_CLOUD_API_URL", cloud_v4_mineru_api)
    monkeypatch.setenv("MINERU_API_TOKEN", "cloud-token")
    monkeypatch.setenv("MINERU_API_POLL_INTERVAL_SECONDS", "0.01")
    cfg.generation["visionCaption"] = {"mode": "off"}

    state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    source_md = (cfg.book_dir / state["sources_md"][0]).read_text(encoding="utf-8")
    assert "Cloud page one." in source_md
    assert 'src="/bookwiki-assets/tiny/figure-1.png"' in source_md
    assert (cfg.work_dir / "assets" / "tiny" / "figure-1.png").read_bytes() == (
        b"\x89PNG\r\n\x1a\ncloud"
    )
    manifest = json.loads(
        (cfg.book_dir / state["source_ref_manifests"][0]).read_text(encoding="utf-8")
    )
    assert manifest["pages"][0]["blocks"][1]["asset_path"] == "work/assets/tiny/figure-1.png"


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


def test_convert_node_adds_vision_caption_to_image_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = default_config(tmp_path / "books" / "mini")
    cfg.input_dir.mkdir(parents=True)
    (cfg.input_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    cfg.llm_runtime = RecordingRuntime(
        [
            {
                "caption_md": "A bell-shaped sampling distribution.",
                "key_points": ["bell shape"],
                "source_ref": "paper-p001",
                "confidence": 0.91,
            }
        ]
    )

    def fake_convert(path: Path, *, source_id: str):
        return {
            "markdown": "raw",
            "content_list_v2": [
                {
                    "page_idx": 0,
                    "items": [
                        {"type": "text", "content": "Normal approximation."},
                        {
                            "type": "image",
                            "asset_path": "work/assets/paper/figure.png",
                            "bbox": [0, 0, 10, 10],
                        },
                    ],
                }
            ],
            "content_list": None,
            "assets": [],
        }

    monkeypatch.setattr("bookwiki.pipeline.nodes.convert_document_to_source", fake_convert)

    state = asyncio.run(convert_node({"book_id": cfg.book_id}, cfg))

    source_md = (cfg.book_dir / state["sources_md"][0]).read_text(encoding="utf-8")
    assert "A bell-shaped sampling distribution." in source_md
    manifest = json.loads(
        (cfg.book_dir / state["source_ref_manifests"][0]).read_text(encoding="utf-8")
    )
    image_block = manifest["pages"][0]["blocks"][1]
    assert image_block["caption"] == "A bell-shaped sampling distribution."
    assert cfg.llm_runtime.calls[0]["output_model"].__name__ == "VisionCaptionResult"
