from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown, source_id_from_stem
from bookwiki.convert.source_normalizer import normalize_structured_source
from bookwiki.scheduler.llm import load_dotenv

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_CLOUD_V4_API_BASE_URL = "https://mineru.net"
# Per-request HTTP timeout. A 1000-page parse can take far longer than the old 20s, and
# this same value also bounds each status poll, so it must be generous.
DEFAULT_TIMEOUT_SECONDS = 1800.0
# Total wall-clock budget for the async parse to finish (separate from the per-request
# HTTP timeout above): a big book keeps polling well past one request's timeout. Falls
# back to ``timeout_seconds`` when neither this nor ``MINERU_API_POLL_DEADLINE_SECONDS``
# is set, preserving the old single-knob behaviour.
DEFAULT_POLL_DEADLINE_SECONDS = 7200.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_MODEL_VERSION = "vlm"
COMPLETED_TASK_STATUSES = {"completed", "success", "succeeded", "done"}
FAILED_TASK_STATUSES = {"failed", "error", "cancelled", "canceled"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


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
    backend: str | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> str:
    return convert_document_to_md(
        path,
        source_id=source_id,
        api_base_url=api_base_url,
        backend=backend,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def convert_document_to_md(
    path: str | Path,
    *,
    source_id: str | None = None,
    api_base_url: str | None = None,
    backend: str | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> str:
    document_path = Path(path)
    resolved_source_id = source_id_from_stem(source_id or document_path.stem)
    result = convert_document_to_source(
        path,
        source_id=resolved_source_id,
        api_base_url=api_base_url,
        backend=backend,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    return normalize_mineru_parse_result(result, source_id=resolved_source_id)


def convert_document_to_source(
    path: str | Path,
    *,
    source_id: str | None = None,
    api_base_url: str | None = None,
    backend: str | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    load_dotenv()
    document_path = Path(path)
    resolved_source_id = source_id_from_stem(source_id or document_path.stem)
    timeout = timeout_seconds or float(
        os.getenv("MINERU_API_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    poll_interval = poll_interval_seconds or float(
        os.getenv("MINERU_API_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS)
    )
    poll_deadline = float(
        os.getenv("MINERU_API_POLL_DEADLINE_SECONDS", DEFAULT_POLL_DEADLINE_SECONDS)
    )
    resolved_backend = _resolve_backend(backend)
    if resolved_backend == "cloud-v4":
        api_url = (
            api_base_url or os.getenv("MINERU_CLOUD_API_URL") or DEFAULT_CLOUD_V4_API_BASE_URL
        ).rstrip("/")
        token = _cloud_api_token()
        try:
            result = _parse_with_cloud_v4(
                document_path,
                api_url,
                token,
                timeout,
                poll_interval,
                resolved_source_id,
                poll_deadline_seconds=poll_deadline,
            )
        except Exception as exc:
            msg = (
                "MinerU cloud-v4 API is required for PDF/PPTX conversion, "
                f"but parsing failed: {exc}"
            )
            raise MineruConversionError(msg) from exc
        result["source_id"] = resolved_source_id
        return result

    api_url = (api_base_url or os.getenv("MINERU_API_URL") or DEFAULT_API_BASE_URL).rstrip("/")

    if not _health_check(api_url, timeout):
        msg = (
            "MinerU API is required for PDF/PPTX conversion, "
            f"but health check failed: {api_url}/health"
        )
        raise MineruConversionError(msg)

    try:
        result = _parse_with_api(
            document_path, api_url, timeout, poll_interval, poll_deadline_seconds=poll_deadline
        )
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


def _resolve_backend(backend: str | None) -> str:
    value = (backend or os.getenv("MINERU_BACKEND") or "local").strip().lower()
    if value in {"local", "mineru-api", "self-hosted", "selfhosted"}:
        return "local"
    if value in {"cloud", "cloud-v4", "mineru-cloud-v4"}:
        return "cloud-v4"
    msg = "MINERU_BACKEND must be one of: local, cloud-v4"
    raise MineruConversionError(msg)


def _cloud_api_token() -> str:
    token = os.getenv("MINERU_API_TOKEN") or os.getenv("MINERU_TOKEN")
    if not token:
        msg = "MINERU_API_TOKEN is required when MINERU_BACKEND=cloud-v4"
        raise MineruConversionError(msg)
    return token


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
    path: Path,
    api_base_url: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    *,
    poll_deadline_seconds: float | None = None,
) -> dict[str, Any]:
    submitted = _submit_task(path, api_base_url, timeout_seconds)
    task_id = _task_id_from_response(submitted)
    result = _wait_for_task_result(
        api_base_url,
        task_id,
        timeout_seconds,
        poll_interval_seconds,
        poll_deadline_seconds=poll_deadline_seconds,
    )
    return _extract_source_from_api_response(result, path.stem)


def _parse_with_cloud_v4(
    path: Path,
    api_base_url: str,
    token: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    source_id: str,
    *,
    poll_deadline_seconds: float | None = None,
) -> dict[str, Any]:
    submitted = _submit_cloud_v4_upload(path, api_base_url, token, timeout_seconds, source_id)
    data = _cloud_response_data(submitted, "signed upload URL request")
    batch_id = _cloud_batch_id(data)
    upload_url = _cloud_upload_url(data)
    _put_file(upload_url, path, timeout_seconds)
    result = _wait_for_cloud_v4_result(
        api_base_url,
        token,
        batch_id,
        path.name,
        timeout_seconds,
        poll_interval_seconds,
        poll_deadline_seconds=poll_deadline_seconds,
    )
    zip_url = _cloud_zip_url(result, path.name)
    payload = _download_bytes(zip_url, timeout_seconds)
    if not _looks_like_zip(payload, ""):
        msg = f"MinerU cloud-v4 result for {path.name!r} was not a zip file"
        raise MineruConversionError(msg)
    return _extract_source_from_zip(payload)


def _submit_cloud_v4_upload(
    path: Path, api_base_url: str, token: str, timeout_seconds: float, source_id: str
) -> dict[str, Any]:
    payload = {
        "files": [{"name": path.name, "data_id": source_id}],
        "model_version": os.getenv("MINERU_MODEL_VERSION", DEFAULT_MODEL_VERSION),
    }
    return _post_json(
        _cloud_v4_url(api_base_url, "/api/v4/file-urls/batch"),
        payload,
        timeout_seconds,
        headers=_cloud_auth_headers(token),
    )


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


def _cloud_v4_url(api_base_url: str, path: str) -> str:
    base = api_base_url.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    if base.endswith("/api/v4"):
        normalized_path = normalized_path.removeprefix("/api/v4")
    return f"{base}{normalized_path}"


def _cloud_auth_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_json(
    url: str, payload: dict[str, Any], timeout_seconds: float, *, headers: dict[str, str]
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response_body = response.read()
    return json.loads(response_body.decode("utf-8", errors="replace"))


def _cloud_response_data(response: dict[str, Any], context: str) -> dict[str, Any]:
    code = response.get("code", 0)
    if code not in {0, "0"}:
        msg = f"MinerU cloud-v4 {context} failed: {response.get('msg') or code}"
        raise MineruConversionError(msg)
    data = response.get("data")
    if not isinstance(data, dict):
        msg = f"MinerU cloud-v4 {context} response did not include data"
        raise MineruConversionError(msg)
    return data


def _cloud_batch_id(data: dict[str, Any]) -> str:
    batch_id = data.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        msg = "MinerU cloud-v4 response did not include batch_id"
        raise MineruConversionError(msg)
    return batch_id


def _cloud_upload_url(data: dict[str, Any]) -> str:
    urls = data.get("file_urls") or data.get("upload_urls")
    if not isinstance(urls, list) or not urls:
        msg = "MinerU cloud-v4 response did not include file_urls"
        raise MineruConversionError(msg)
    first = urls[0]
    if isinstance(first, str) and first:
        return first
    if isinstance(first, dict):
        for key in ("upload_url", "file_url", "url"):
            value = first.get(key)
            if isinstance(value, str) and value:
                return value
    msg = "MinerU cloud-v4 response did not include an upload URL"
    raise MineruConversionError(msg)


def _put_file(upload_url: str, path: Path, timeout_seconds: float) -> None:
    payload = path.read_bytes()
    parsed = urllib.parse.urlsplit(upload_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = f"MinerU cloud-v4 upload URL is invalid: {upload_url!r}"
        raise MineruConversionError(msg)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_cls(parsed.netloc, timeout=timeout_seconds)
    try:
        connection.request(
            "PUT",
            target,
            body=payload,
            headers={"Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        response.read()
        if response.status >= 400:
            msg = f"MinerU cloud-v4 file upload failed with HTTP {response.status}"
            raise MineruConversionError(msg)
    finally:
        connection.close()


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
    *,
    poll_deadline_seconds: float | None = None,
) -> dict[str, Any]:
    effective_deadline_seconds = poll_deadline_seconds or timeout_seconds
    deadline = time.monotonic() + effective_deadline_seconds
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

    msg = f"MinerU async task {task_id} timed out after {effective_deadline_seconds:g}s"
    if last_status:
        msg += f" (last status: {last_status})"
    raise MineruConversionError(msg)


def _wait_for_cloud_v4_result(
    api_base_url: str,
    token: str,
    batch_id: str,
    file_name: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    *,
    poll_deadline_seconds: float | None = None,
) -> dict[str, Any]:
    effective_deadline_seconds = poll_deadline_seconds or timeout_seconds
    deadline = time.monotonic() + effective_deadline_seconds
    last_status: str | None = None
    url = _cloud_v4_url(api_base_url, f"/api/v4/extract-results/batch/{batch_id}")
    headers = _cloud_auth_headers(token)
    while time.monotonic() < deadline:
        status_data = _get_json(url, timeout_seconds, headers=headers)
        data = _cloud_response_data(status_data, "batch status request")
        result = _cloud_extract_result(data, file_name)
        status = str(result.get("state", "")).lower()
        last_status = status or last_status
        if status in COMPLETED_TASK_STATUSES:
            return result
        if status in FAILED_TASK_STATUSES:
            msg = f"MinerU cloud-v4 batch {batch_id} failed: {result.get('err_msg') or status}"
            raise MineruConversionError(msg)
        time.sleep(max(poll_interval_seconds, 0.01))

    msg = f"MinerU cloud-v4 batch {batch_id} timed out after {effective_deadline_seconds:g}s"
    if last_status:
        msg += f" (last status: {last_status})"
    raise MineruConversionError(msg)


def _cloud_extract_result(data: dict[str, Any], file_name: str) -> dict[str, Any]:
    results = data.get("extract_result")
    if isinstance(results, dict):
        return results
    if not isinstance(results, list) or not results:
        msg = "MinerU cloud-v4 batch response did not include extract_result"
        raise MineruConversionError(msg)
    for item in results:
        if isinstance(item, dict) and item.get("file_name") == file_name:
            return item
    for item in results:
        if isinstance(item, dict):
            return item
    msg = "MinerU cloud-v4 batch response did not include a usable extract_result"
    raise MineruConversionError(msg)


def _cloud_zip_url(result: dict[str, Any], file_name: str) -> str:
    for key in ("full_zip_url", "zip_url"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    msg = f"MinerU cloud-v4 result did not include full_zip_url for {file_name!r}"
    raise MineruConversionError(msg)


def _get_json(
    url: str, timeout_seconds: float, *, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers=headers or {"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8", errors="replace"))


def _download_bytes(url: str, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/zip, application/octet-stream, */*"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


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
    assets: list[dict[str, Any]] = []
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/").lower()
            if normalized.endswith(".md") and markdown is None:
                markdown = archive.read(name).decode("utf-8", errors="replace")
                continue
            if Path(normalized).suffix in IMAGE_SUFFIXES:
                assets.append(
                    {
                        "archive_path": name.replace("\\", "/"),
                        "filename": Path(name).name,
                        "data": archive.read(name),
                    }
                )
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
        "assets": assets,
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
            return {"markdown": item, "content_list_v2": None, "content_list": None, "assets": []}
        if isinstance(item, dict):
            markdown = _markdown_from_mapping(item)
            if markdown is not None:
                return {
                    "markdown": markdown,
                    "content_list_v2": item.get("content_list_v2"),
                    "content_list": item.get("content_list"),
                    "assets": item.get("assets") or [],
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
