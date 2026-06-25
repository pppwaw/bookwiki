"""Compile-check generated MDX against the same parser config as the site.

The fumadocs site parses chapter ``.mdx`` with ``remark-math`` enabled (see
``site-template/source.config.ts``). A single MDX-significant slip the model leaves
in prose - e.g. a comparison like ``n<30`` (read as a JSX tag ``<3...>``) or set
notation ``{z ≥ a}`` (read as a JS expression) - makes the strict MDX parser fail
and breaks the whole site build. This module shells out to the bundled Node
validator (``tools/mdx-validate``), which runs ``@mdx-js/mdx`` ``compile`` with the
SAME ``remark-math`` plugin, so the check matches what the site will accept (and
does not false-positive on math like ``$\\bar{X}$``).

It is best-effort: if Node or the validator's ``node_modules`` is unavailable, it
logs a warning and returns no diagnostics rather than failing the pipeline on a
missing dev tool.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

_VALIDATOR = Path(__file__).resolve().parents[2] / "tools" / "mdx-validate" / "validate.mjs"


def mdx_validator_available() -> bool:
    """Whether Node and the validator's installed dependencies are both present."""
    return (
        shutil.which("node") is not None
        and _VALIDATOR.exists()
        and (_VALIDATOR.parent / "node_modules").exists()
    )


def validate_mdx(content: str, *, timeout_s: float = 30.0) -> list[str]:
    """Return MDX parse errors for ``content`` (empty list means it compiles).

    Each error is a human-readable ``"line L, column C: message"`` string suitable
    for feeding back to a repair agent.
    """
    node = shutil.which("node")
    if node is None or not _VALIDATOR.exists() or not (_VALIDATOR.parent / "node_modules").exists():
        LOGGER.warning(
            "mdx validator unavailable (node=%s, script_exists=%s); skipping MDX check",
            node is not None,
            _VALIDATOR.exists(),
        )
        return []

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, content via stdin
            [node, str(_VALIDATOR)],
            input=content,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOGGER.warning("mdx validator failed to run: %s; skipping MDX check", exc)
        return []

    if proc.returncode != 0:
        LOGGER.warning(
            "mdx validator internal error (rc=%s): %s; skipping MDX check",
            proc.returncode,
            (proc.stderr or "")[-500:],
        )
        return []

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        LOGGER.warning("mdx validator returned non-JSON output; skipping MDX check")
        return []

    if data.get("ok"):
        return []
    errors = [_format_error(error) for error in data.get("errors", []) if isinstance(error, dict)]
    if errors:
        preview = errors[0][:120]
        suffix = "..." if len(errors) > 1 else ""
        LOGGER.debug(
            "mdx validation found %d error(s): %s%s", len(errors), preview, suffix
        )
    return errors


def validate_mdx_many(
    files: dict[str, str] | list[tuple[str, str]],
    *,
    timeout_s: float = 120.0,
    max_files: int = 32,
    max_bytes: int = 8_000_000,
) -> dict[str, list[str]]:
    """Validate many MDX files with as few Node cold-starts as possible.

    ``files`` maps an identifier (typically a path) to MDX content. Returns the same keys
    mapped to their parse-error lists (empty == compiles). Files are grouped into batches
    (capped by ``max_files`` and ``max_bytes``) and each batch is one ``node`` process.
    When a batch's output is unparseable (a pathological file crashed the validator), the
    batch is split in half and retried so one bad file cannot blank out its neighbours;
    a lone offender falls back to the single-file path (which best-effort skips it).

    Mirrors ``validate_mdx``'s best-effort contract: if Node / the validator is
    unavailable, it logs a warning and returns empty lists rather than failing the run.
    """
    items: list[tuple[str, str]] = list(files.items()) if isinstance(files, dict) else list(files)
    out: dict[str, list[str]] = {key: [] for key, _ in items}
    if not items:
        return out
    if not mdx_validator_available():
        LOGGER.warning(
            "mdx validator unavailable (node=%s, script_exists=%s); skipping MDX check",
            shutil.which("node") is not None,
            _VALIDATOR.exists(),
        )
        return out

    for batch in _batch_items(items, max_files=max_files, max_bytes=max_bytes):
        _run_batch(batch, out, timeout_s=timeout_s)
    return out


def _batch_items(
    items: list[tuple[str, str]], *, max_files: int, max_bytes: int
) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for key, content in items:
        content_bytes = len(content.encode("utf-8"))
        if current and (len(current) >= max_files or size + content_bytes > max_bytes):
            batches.append(current)
            current, size = [], 0
        current.append((key, content))
        size += content_bytes
    if current:
        batches.append(current)
    return batches


def _run_batch(
    batch: list[tuple[str, str]], out: dict[str, list[str]], *, timeout_s: float
) -> None:
    results = _invoke_batch([content for _, content in batch], timeout_s=timeout_s)
    if results is None or len(results) != len(batch):
        if len(batch) == 1:
            key, content = batch[0]
            out[key] = validate_mdx(content, timeout_s=timeout_s)
            return
        mid = len(batch) // 2
        _run_batch(batch[:mid], out, timeout_s=timeout_s)
        _run_batch(batch[mid:], out, timeout_s=timeout_s)
        return
    for (key, _content), result in zip(batch, results, strict=True):
        if not isinstance(result, dict) or result.get("ok"):
            out[key] = []
            continue
        out[key] = [
            _format_error(error)
            for error in result.get("errors", [])
            if isinstance(error, dict)
        ]


def _invoke_batch(contents: list[str], *, timeout_s: float) -> list[Any] | None:
    node = shutil.which("node")
    if node is None:
        return None
    payload = json.dumps(
        {
            "files": [
                {"path": str(index), "content": content}
                for index, content in enumerate(contents)
            ]
        }
    )
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, content via stdin
            [node, str(_VALIDATOR), "--batch"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOGGER.warning("mdx validator batch failed to run: %s", exc)
        return None
    if proc.returncode != 0:
        LOGGER.warning("mdx validator batch internal error (rc=%s)", proc.returncode)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else None


def _format_error(error: dict[str, object]) -> str:
    message = str(error.get("message") or "MDX parse error")
    line = error.get("line")
    column = error.get("column")
    if isinstance(line, int):
        if isinstance(column, int):
            return f"line {line}, column {column}: {message}"
        return f"line {line}: {message}"
    return message
