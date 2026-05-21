from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown, source_id_from_stem
from bookwiki.utils.logging import get_logger

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_VLM_SERVER_URL = "http://127.0.0.1:30000"
DEFAULT_TIMEOUT_SECONDS = 20.0

logger = get_logger(__name__)


class MineruConversionError(RuntimeError):
    pass


def normalize_mineru_markdown(raw_md: str, *, source_id: str) -> str:
    cleaned = clean_markdown(raw_md)
    if not cleaned:
        cleaned = "No extractable text was returned by MinerU."

    if SOURCE_REF_RE.search(cleaned):
        return f"# {source_id}\n\n{cleaned}\n"

    page_chunks = [chunk for chunk in _split_pages(raw_md) if chunk.strip()]
    if not page_chunks:
        page_chunks = [cleaned]

    blocks = [f"# {source_id}"]
    for page_index, chunk in enumerate(page_chunks, start=1):
        page_body = clean_markdown(chunk)
        blocks.append(f"<!-- source_ref: {source_id}-p{page_index:03d} -->\n\n{page_body}")
    return "\n\n".join(blocks).strip() + "\n"


def convert_pdf_to_md(
    path: str | Path,
    *,
    source_id: str | None = None,
    api_base_url: str | None = None,
    vlm_server_url: str | None = None,
    timeout_seconds: float | None = None,
    do_parse_func: Callable[..., Any] | None = None,
) -> str:
    pdf_path = Path(path)
    resolved_source_id = source_id_from_stem(source_id or pdf_path.stem)
    timeout = timeout_seconds or float(
        os.getenv("MINERU_API_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    api_url = (api_base_url or os.getenv("MINERU_API_URL") or DEFAULT_API_BASE_URL).rstrip("/")
    vlm_url = (vlm_server_url or os.getenv("MINERU_VLM_URL") or DEFAULT_VLM_SERVER_URL).rstrip("/")

    if _env_flag("MINERU_API_DISABLED"):
        logger.warning("MinerU API disabled; falling back to MinerU pipeline backend")
        return normalize_mineru_markdown(
            _convert_with_pipeline(pdf_path, do_parse_func=do_parse_func),
            source_id=resolved_source_id,
        )

    if _health_check(api_url, timeout):
        try:
            raw_md = _parse_with_api(pdf_path, api_url, timeout)
            return normalize_mineru_markdown(raw_md, source_id=resolved_source_id)
        except Exception as exc:
            logger.warning(
                "MinerU API parse failed: %s; trying MinerU vlm-http-client backend", exc
            )
            try:
                raw_md = _convert_with_vlm_client(
                    pdf_path, do_parse_func=do_parse_func, server_url=vlm_url
                )
                return normalize_mineru_markdown(raw_md, source_id=resolved_source_id)
            except Exception as vlm_exc:
                logger.warning(
                    "MinerU vlm-http-client backend unavailable: %s; "
                    "falling back to MinerU pipeline backend",
                    vlm_exc,
                )
    else:
        logger.warning("MinerU health check failed; falling back to MinerU pipeline backend")

    return normalize_mineru_markdown(
        _convert_with_pipeline(pdf_path, do_parse_func=do_parse_func, server_url=vlm_url),
        source_id=resolved_source_id,
    )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _split_pages(raw_md: str) -> list[str]:
    if "\x0c" in raw_md:
        return raw_md.split("\x0c")
    return [raw_md]


def _health_check(api_base_url: str, timeout_seconds: float) -> bool:
    request = urllib.request.Request(f"{api_base_url}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                return False
            if not body.strip():
                return True
            data = json.loads(body)
            return str(data.get("status", "healthy")).lower() in {"healthy", "ok", "ready"}
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return False


def _parse_with_api(path: Path, api_base_url: str, timeout_seconds: float) -> str:
    body, content_type = _multipart_body(
        fields={
            "return_md": "true",
            "response_format_zip": "false",
            "return_original_file": "false",
        },
        files={"files": path},
    )
    request = urllib.request.Request(
        f"{api_base_url}/file_parse",
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
    data = json.loads(payload.decode("utf-8", errors="replace"))
    return _extract_markdown_from_api_response(data, path.stem)


def _multipart_body(*, fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"bookwiki-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ]
        )
    for name, path in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{path.name}"\r\n'
                ).encode(),
                b"Content-Type: application/pdf\r\n\r\n",
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _extract_markdown_from_api_response(data: dict[str, Any], stem: str) -> str:
    for key in ("md_content", "markdown", "md"):
        value = data.get(key)
        if isinstance(value, str):
            return value

    results = data.get("results")
    if isinstance(results, dict):
        candidates = [results.get(stem), *results.values()]
        for item in candidates:
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                for key in ("md_content", "markdown", "md", "content"):
                    value = item.get(key)
                    if isinstance(value, str):
                        return value

    msg = f"MinerU API response did not include markdown for {stem!r}"
    raise MineruConversionError(msg)


def _convert_with_pipeline(
    path: Path,
    *,
    do_parse_func: Callable[..., Any] | None = None,
    server_url: str = DEFAULT_VLM_SERVER_URL,
) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="bookwiki-mineru-") as tmp:
            return _run_do_parse(
                path,
                Path(tmp),
                backend="pipeline",
                server_url=server_url,
                do_parse_func=do_parse_func,
            )
    except Exception as exc:
        logger.warning("MinerU pipeline backend unavailable; using metadata fallback: %s", exc)
        size = path.stat().st_size if path.exists() else 0
        return (
            f"PDF file: {path.name}\n\n"
            f"Size: {size} bytes.\n\n"
            "MinerU pipeline was not available in this environment."
        )


def _convert_with_vlm_client(
    path: Path,
    *,
    do_parse_func: Callable[..., Any] | None = None,
    server_url: str = DEFAULT_VLM_SERVER_URL,
) -> str:
    with tempfile.TemporaryDirectory(prefix="bookwiki-mineru-vlm-") as tmp:
        return _run_do_parse(
            path,
            Path(tmp),
            backend="vlm-http-client",
            server_url=server_url,
            do_parse_func=do_parse_func,
        )


def _run_do_parse(
    path: Path,
    output_dir: Path,
    *,
    backend: str,
    server_url: str,
    do_parse_func: Callable[..., Any] | None,
) -> str:
    func = do_parse_func or _import_do_parse()
    result = _call_do_parse(func, path, output_dir, backend=backend, server_url=server_url)
    if isinstance(result, Path):
        return result.read_text(encoding="utf-8", errors="ignore")
    if isinstance(result, str):
        if "\n" not in result and len(result) < 260 and Path(result).exists():
            return Path(result).read_text(encoding="utf-8", errors="ignore")
        return result

    md_files = sorted(output_dir.rglob("*.md"))
    if md_files:
        return md_files[0].read_text(encoding="utf-8", errors="ignore")

    msg = f"do_parse did not return or write markdown for {path.name}"
    raise MineruConversionError(msg)


def _import_do_parse() -> Callable[..., Any]:
    from mineru.cli.common import do_parse

    return do_parse


def _call_do_parse(
    func: Callable[..., Any],
    path: Path,
    output_dir: Path,
    *,
    backend: str,
    server_url: str,
) -> Any:
    attempts = [
        lambda: func(
            path=str(path), output_dir=str(output_dir), backend=backend, server_url=server_url
        ),
        lambda: func(
            input_path=str(path), output_dir=str(output_dir), backend=backend, server_url=server_url
        ),
        lambda: func(str(path), str(output_dir), backend=backend, server_url=server_url),
    ]
    last_error: TypeError | None = None
    for call in attempts:
        try:
            return call()
        except TypeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return None
