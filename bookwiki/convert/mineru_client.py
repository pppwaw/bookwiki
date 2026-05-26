from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown, source_id_from_stem
from bookwiki.convert.source_normalizer import normalize_structured_source

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
COMPLETED_TASK_STATUSES = {"completed", "success", "succeeded", "done"}
FAILED_TASK_STATUSES = {"failed", "error", "cancelled", "canceled"}


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
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> str:
    return convert_document_to_md(
        path,
        source_id=source_id,
        api_base_url=api_base_url,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def convert_document_to_md(
    path: str | Path,
    *,
    source_id: str | None = None,
    api_base_url: str | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> str:
    document_path = Path(path)
    resolved_source_id = source_id_from_stem(source_id or document_path.stem)
    result = convert_document_to_source(
        path,
        source_id=resolved_source_id,
        api_base_url=api_base_url,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return normalize_mineru_parse_result(result, source_id=resolved_source_id)


def convert_document_to_source(
    path: str | Path,
    *,
    source_id: str | None = None,
    api_base_url: str | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    document_path = Path(path)
    resolved_source_id = source_id_from_stem(source_id or document_path.stem)
    timeout = timeout_seconds or float(
        os.getenv("MINERU_API_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    poll_interval = poll_interval_seconds or float(
        os.getenv("MINERU_API_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS)
    )
    api_url = (api_base_url or os.getenv("MINERU_API_URL") or DEFAULT_API_BASE_URL).rstrip("/")

    if not _health_check(api_url, timeout):
        msg = (
            "MinerU API is required for PDF/PPTX conversion, "
            f"but health check failed: {api_url}/health"
        )
        raise MineruConversionError(msg)

    try:
        result = _parse_with_api(document_path, api_url, timeout, poll_interval)
    except Exception as exc:
        msg = (
            "MinerU API is required for PDF/PPTX conversion, "
            f"but async task parsing failed: {exc}"
        )
        raise MineruConversionError(msg) from exc
    result["source_id"] = resolved_source_id
    return result


def normalize_mineru_parse_result(data: dict[str, Any], *, source_id: str) -> str:
    normalized = normalize_structured_source(
        raw_md=str(data.get("markdown") or data.get("md_content") or data.get("md") or ""),
        source_id=source_id,
        content_list_v2=data.get("content_list_v2"),
        content_list=data.get("content_list"),
    )
    return normalized.markdown


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


def _parse_with_api(
    path: Path, api_base_url: str, timeout_seconds: float, poll_interval_seconds: float
) -> dict[str, Any]:
    submitted = _submit_task(path, api_base_url, timeout_seconds)
    task_id = _task_id_from_response(submitted)
    result = _wait_for_task_result(api_base_url, task_id, timeout_seconds, poll_interval_seconds)
    return _extract_source_from_api_response(result, path.stem)


def _submit_task(path: Path, api_base_url: str, timeout_seconds: float) -> dict[str, Any]:
    body, content_type = _multipart_body(
        fields={
            "return_md": "true",
            "return_content_list": "true",
            "response_format_zip": "true",
            "return_original_file": "false",
        },
        files={"files": path},
    )
    request = urllib.request.Request(
        f"{api_base_url}/tasks",
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8", errors="replace"))


def _task_id_from_response(data: dict[str, Any]) -> str:
    task_id = data.get("task_id") or data.get("id")
    if not isinstance(task_id, str) or not task_id:
        msg = "MinerU async task response did not include task_id"
        raise MineruConversionError(msg)
    return task_id


def _wait_for_task_result(
    api_base_url: str,
    task_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    while time.monotonic() < deadline:
        status_data = _get_json(f"{api_base_url}/tasks/{task_id}", timeout_seconds)
        status = str(status_data.get("status", "")).lower()
        last_status = status or last_status
        if status in COMPLETED_TASK_STATUSES:
            return _get_task_result(f"{api_base_url}/tasks/{task_id}/result", timeout_seconds)
        if status in FAILED_TASK_STATUSES:
            msg = f"MinerU async task {task_id} failed: {status_data.get('error') or status}"
            raise MineruConversionError(msg)
        time.sleep(max(poll_interval_seconds, 0.01))

    msg = f"MinerU async task {task_id} timed out after {timeout_seconds:g}s"
    if last_status:
        msg += f" (last status: {last_status})"
    raise MineruConversionError(msg)


def _get_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8", errors="replace"))


def _get_task_result(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json, application/zip, application/octet-stream"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
        content_type = response.headers.get("Content-Type", "")
    if _looks_like_zip(payload, content_type):
        return _extract_source_from_zip(payload)
    return json.loads(payload.decode("utf-8", errors="replace"))


def _looks_like_zip(payload: bytes, content_type: str) -> bool:
    return payload.startswith(b"PK\x03\x04") or "zip" in content_type.lower()


def _extract_source_from_zip(payload: bytes) -> dict[str, Any]:
    markdown: str | None = None
    content_list_v2: Any | None = None
    content_list: Any | None = None
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/").lower()
            if normalized.endswith(".md") and markdown is None:
                markdown = archive.read(name).decode("utf-8", errors="replace")
                continue
            if not normalized.endswith(".json"):
                continue
            try:
                data = json.loads(archive.read(name).decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if "content_list_v2" in normalized:
                content_list_v2 = data
            elif "content_list" in normalized:
                content_list = data
            elif isinstance(data, dict):
                content_list_v2 = data.get("content_list_v2", content_list_v2)
                content_list = data.get("content_list", content_list)
    if markdown is None:
        msg = "MinerU zip result did not include a markdown file"
        raise MineruConversionError(msg)
    return {
        "markdown": markdown,
        "content_list_v2": content_list_v2,
        "content_list": content_list,
    }


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
                f"Content-Type: {_content_type_for(path)}\r\n\r\n".encode(),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _content_type_for(path: Path) -> str:
    if path.suffix.lower() == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return "application/pdf"


def _extract_markdown_from_api_response(data: dict[str, Any], stem: str) -> str:
    return str(_extract_source_from_api_response(data, stem)["markdown"])


def _extract_source_from_api_response(data: dict[str, Any], stem: str) -> dict[str, Any]:
    for item in _api_response_candidates(data, stem):
        if isinstance(item, str):
            return {"markdown": item, "content_list_v2": None, "content_list": None}
        if isinstance(item, dict):
            markdown = _markdown_from_mapping(item)
            if markdown is not None:
                return {
                    "markdown": markdown,
                    "content_list_v2": item.get("content_list_v2"),
                    "content_list": item.get("content_list"),
                }

    msg = f"MinerU API response did not include markdown for {stem!r}"
    raise MineruConversionError(msg)


def _api_response_candidates(data: dict[str, Any], stem: str) -> list[Any]:
    candidates: list[Any] = [data]
    results = data.get("results")
    if isinstance(results, dict):
        if stem in results:
            candidates.append(results[stem])
        candidates.extend(results.values())
    elif isinstance(results, list):
        candidates.extend(results)
    return candidates


def _markdown_from_mapping(data: dict[str, Any]) -> str | None:
    for key in ("md_content", "markdown", "md"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    value = data.get("content")
    return value if isinstance(value, str) else None
