"""Extract inline quiz structure from generated MDX via the bundled Node helper.

BookWiki authors knowledge quizzes inline (a full ``<QuizBlock>`` with ``<QuizItem>``s
written directly into the section prose) and marks application quizzes with item-level
``<QuizItemSlot ... />`` placeholders inside an authored ``<QuizBlock>``. The sanitizer
(:mod:`bookwiki.generate.inline_quiz`) needs a trustworthy structural read of those tags
— answers, choice ids, citations, slot specs — plus exact source offsets so it can drop
or rewrite blocks in place.

Unlike :mod:`bookwiki.checkers.mdx_validator` (which degrades to "no diagnostics" when the
toolchain is missing), this extractor is a safety net: if it cannot run, callers MUST fail
loud rather than ship unvalidated quizzes. So toolchain/parse failures raise
:class:`QuizExtractError` instead of returning an empty result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bookwiki.utils.logging import get_logger

LOGGER = get_logger(__name__)

_EXTRACTOR = Path(__file__).resolve().parents[2] / "tools" / "mdx-validate" / "extract-quiz.mjs"


class QuizExtractError(RuntimeError):
    """Raised when inline quizzes cannot be extracted (toolchain or parse failure)."""


def quiz_extractor_available() -> bool:
    """Whether Node and the extractor's installed dependencies are both present."""
    return (
        shutil.which("node") is not None
        and _EXTRACTOR.exists()
        and (_EXTRACTOR.parent / "node_modules").exists()
    )


def _run_extractor(content: str, *, timeout_s: float) -> dict[str, Any]:
    """Run the Node extractor on ``content`` and return its parsed JSON payload.

    Raises :class:`QuizExtractError` if the Node toolchain is unavailable, times out, returns
    non-JSON, or the MDX cannot be parsed.
    """
    node = shutil.which("node")
    if node is None or not _EXTRACTOR.exists() or not (_EXTRACTOR.parent / "node_modules").exists():
        raise QuizExtractError(
            f"quiz extractor unavailable (node={node is not None}, "
            f"script_exists={_EXTRACTOR.exists()}); cannot validate inline quizzes"
        )

    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, content via stdin
            [node, str(_EXTRACTOR)],
            input=content,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise QuizExtractError(f"quiz extractor failed to run: {exc}") from exc

    if proc.returncode != 0:
        raise QuizExtractError(
            f"quiz extractor internal error (rc={proc.returncode}): {(proc.stderr or '')[-500:]}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QuizExtractError("quiz extractor returned non-JSON output") from exc

    if not data.get("ok"):
        errors = "; ".join(str(error) for error in data.get("errors", []))
        raise QuizExtractError(f"quiz extractor could not parse MDX: {errors}")
    return data


def extract_inline_quizzes(content: str, *, timeout_s: float = 30.0) -> list[dict[str, Any]]:
    """Return the inline ``<QuizBlock>`` structures found in ``content``.

    Each block is ``{"start": int, "end": int, "children": [...]}`` where children are
    ``kind="item"`` (authored knowledge), ``kind="slot"`` (application placeholder), or
    ``kind="unknown"`` (stray JSX inside a block). Offsets index into ``content``.

    Raises :class:`QuizExtractError` if the Node toolchain is unavailable, times out, or
    the MDX cannot be parsed.
    """
    blocks = _run_extractor(content, timeout_s=timeout_s).get("blocks", [])
    if not isinstance(blocks, list):
        raise QuizExtractError("quiz extractor returned malformed blocks")
    return blocks


def extract_quiz_layout(
    content: str, *, timeout_s: float = 30.0
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"blocks": [...], "stray_slots": [...]}`` parsed from ``content`` via remark AST.

    ``blocks`` is as :func:`extract_inline_quizzes`; ``stray_slots`` are ``<QuizItemSlot/>`` tags
    that sit OUTSIDE any ``<QuizBlock>`` (each ``{"id": str|None, "start": int, "end": int}``).
    The integrate-time slot resolver uses these AST offsets to splice slots precisely — a regex
    over the raw text mis-parses a ``>`` inside an attribute value (e.g. ``topic="... t>0"``) and
    silently leaves the slot in place, leaking its raw tag to the build.

    Raises :class:`QuizExtractError` on toolchain/parse failure (callers may fall back).
    """
    data = _run_extractor(content, timeout_s=timeout_s)
    blocks = data.get("blocks", [])
    stray = data.get("straySlots", [])
    if not isinstance(blocks, list) or not isinstance(stray, list):
        raise QuizExtractError("quiz extractor returned malformed layout")
    return {"blocks": blocks, "stray_slots": stray}
