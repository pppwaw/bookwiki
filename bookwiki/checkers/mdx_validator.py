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
import re
import shutil
import subprocess
from pathlib import Path

from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

_VALIDATOR = Path(__file__).resolve().parents[2] / "tools" / "mdx-validate" / "validate.mjs"

# Contract: every agent prompt mandates ``$...$`` / ``$$...$$`` math delimiters and forbids
# ``\( ... \)`` / ``\[ ... \]``. A bare (single-backslash) ``\[`` / ``\(`` never renders as
# math under remark-math — it shows as a literal bracket, a SILENT failure the MDX compiler
# does not catch. This deterministic check flags it so the repair loop fixes it.
_FORBIDDEN_DELIM_RE = re.compile(r"(?<!\\)\\[\[(]")
# Spans where a backslash is expected and must NOT be flagged: code fences, inline code, and
# already-delimited math (``$...$`` / ``$$...$$``).
_DELIM_MASK_SPAN_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$")


def _mask_delim_spans(content: str) -> str:
    """Blank out code/math spans (preserving length and newlines) so the delimiter scan
    sees only prose/JSX and keeps accurate line numbers."""
    return _DELIM_MASK_SPAN_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), content)


def find_forbidden_latex_delimiters(content: str) -> list[str]:
    """Return errors for contract-forbidden ``\\[`` / ``\\(`` math delimiters in ``content``.

    Pure Python (no Node), so it guards every pipeline level uniformly and even when the
    bundled MDX compiler is unavailable. ``\\\\[`` (a LaTeX linebreak / JSON-escaped
    backslash, e.g. inside a ``citations`` quote) is intentionally NOT flagged.
    """
    masked = _mask_delim_spans(content)
    errors: list[str] = []
    for match in _FORBIDDEN_DELIM_RE.finditer(masked):
        line = content.count("\n", 0, match.start()) + 1
        delim = content[match.start() : match.start() + 2]
        errors.append(
            f"line {line}: forbidden LaTeX delimiter {delim} - use $...$ or $$...$$ "
            "(every agent prompt forbids \\( \\) and \\[ \\])"
        )
    return errors


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
    # Deterministic, Node-independent contract check runs unconditionally, so it still
    # guards content when the bundled compiler is unavailable.
    delimiter_errors = find_forbidden_latex_delimiters(content)

    node = shutil.which("node")
    if node is None or not _VALIDATOR.exists() or not (_VALIDATOR.parent / "node_modules").exists():
        LOGGER.warning(
            "mdx validator unavailable (node=%s, script_exists=%s); skipping MDX compile check",
            node is not None,
            _VALIDATOR.exists(),
        )
        return delimiter_errors

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
        LOGGER.warning("mdx validator failed to run: %s; skipping MDX compile check", exc)
        return delimiter_errors

    if proc.returncode != 0:
        LOGGER.warning(
            "mdx validator internal error (rc=%s): %s; skipping MDX compile check",
            proc.returncode,
            (proc.stderr or "")[-500:],
        )
        return delimiter_errors

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        LOGGER.warning("mdx validator returned non-JSON output; skipping MDX compile check")
        return delimiter_errors

    if data.get("ok"):
        return delimiter_errors
    errors = [_format_error(error) for error in data.get("errors", []) if isinstance(error, dict)]
    errors = delimiter_errors + errors
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
