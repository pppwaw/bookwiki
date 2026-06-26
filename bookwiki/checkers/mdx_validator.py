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


def _format_error(error: dict[str, object]) -> str:
    message = str(error.get("message") or "MDX parse error")
    line = error.get("line")
    column = error.get("column")
    if isinstance(line, int):
        if isinstance(column, int):
            return f"line {line}, column {column}: {message}"
        return f"line {line}: {message}"
    return message
