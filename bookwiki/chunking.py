"""Heading-aware source chunking for stages that must read more than one model
context window of text (``structure`` summarisation, per-chapter skeleton extraction).

The whole point is to never silently overflow: ``chunk_budget`` derives a per-chunk
token cap *strictly below* ``input_token_budget(model)`` so a chunk can never trip
``compact_input``'s per-field truncation. ``chunk_by_heading`` splits the text by
heading level recursively — try the shallowest heading level first, and only re-split a
piece at a deeper level when it is still over budget — bottoming out in a paragraph/char
fallback (with overlap) for a single oversized leaf that has no finer heading.

Each :class:`Chunk` remembers its ``heading_path`` (so downstream code re-groups
sub-chunks back under their real chapter, rather than minting a chapter per slide title)
and the ``source_refs`` it covers (so a deterministic coverage audit can prove no
``<!-- source_ref -->`` was dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bookwiki.convert.common import SOURCE_REF_RE
from bookwiki.scheduler.llm import count_text_tokens, input_token_budget

# Headroom subtracted from the model's per-field budget before applying the stage
# factor: leaves room for the prompt template, book_notes, draft, and the
# ``[truncated]`` suffix so the assembled prompt stays safely under the window.
_PROMPT_OVERHEAD = 8_000

# Per-stage fraction of the usable budget. ``structure`` summaries emit almost nothing,
# so chunks can be large (fewer seams → cleaner chapter boundaries); ``skeleton`` also
# ships a candidate list + registry, so leave more slack.
_STAGE_FACTOR: dict[str, float] = {"structure": 0.7, "skeleton": 0.6}

# Fenced code openers — a ``#`` inside a fence is code, not a heading.
_FENCE_RE = re.compile(r"^(```+|~~~+)")
_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*$")


@dataclass(frozen=True)
class Chunk:
    """One slice of source text, sized to fit a model call.

    ``heading_path`` is the chain of enclosing headings (e.g.
    ``["第3章 神经网络", "3.2 反向传播"]``); regroup sub-chunks by its chapter-level
    (H1/H2) prefix, not by the level a chunk happened to be split at. ``char_start`` /
    ``char_end`` index into the *original* full text. ``source_refs`` are the
    ``<!-- source_ref -->`` ids covered by this chunk (for coverage auditing).
    """

    text: str
    heading_path: list[str]
    char_start: int
    char_end: int
    source_refs: list[str] = field(default_factory=list)


def chunk_budget(model: str, *, stage: str) -> int:
    """Per-chunk token cap for ``stage``, derived from the model's input budget.

    Always strictly below ``input_token_budget(model)`` (minus prompt overhead) so a
    chunk can never trigger ``compact_input``'s silent per-field truncation — the very
    failure mode this chunking exists to prevent. Scales automatically with the model:
    register a window in ``_MODEL_CONTEXT_WINDOW`` and chunk sizes follow.
    """
    try:
        factor = _STAGE_FACTOR[stage]
    except KeyError as exc:
        msg = f"unknown chunking stage {stage!r}; expected one of {sorted(_STAGE_FACTOR)}"
        raise ValueError(msg) from exc
    cap = input_token_budget(model)
    usable = cap - _PROMPT_OVERHEAD
    budget = int(usable * factor)
    if budget < 1:
        # Pathologically small context window: fall back to whatever headroom exists
        # rather than returning a non-positive (and thus unusable) budget.
        budget = max(1, usable)
    # Invariant: never let a chunk reach the per-field truncation threshold.
    if budget >= cap:
        msg = (
            f"chunk_budget(model={model!r}, stage={stage!r}) = {budget} must stay below "
            f"the model's input_token_budget ({cap}); refusing to emit a chunk that would "
            "trip compact_input's silent truncation."
        )
        raise ValueError(msg)
    return budget


def chunk_by_heading(
    text: str, *, model: str, stage: str, budget: int | None = None
) -> list[Chunk]:
    """Split ``text`` into chunks each ``<= chunk_budget(model, stage)`` tokens.

    Recursively splits at the shallowest heading level present; a piece still over
    budget is re-split at the next level down; a leaf with no finer heading falls back
    to a paragraph/char split with overlap. The union of returned ``source_refs`` equals
    the set of refs in ``text`` (callers can assert this for a coverage guarantee).

    ``budget`` overrides the derived per-chunk token cap; leave it ``None`` for the
    normal ``chunk_budget(model, stage)`` value. An explicit (smaller) budget is the
    hook for re-splitting a pathologically large single chapter, and lets tests exercise
    the recursion without synthesising hundreds of thousands of tokens.
    """
    if budget is None:
        budget = chunk_budget(model, stage=stage)
    overlap_tokens = min(1_000, max(1, budget // 50))
    chunks: list[Chunk] = []
    _split_segment(
        text,
        abs_start=0,
        heading_path=[],
        model=model,
        budget=budget,
        overlap_tokens=overlap_tokens,
        out=chunks,
    )
    return [c for c in chunks if c.text.strip()]


def _split_segment(
    text: str,
    *,
    abs_start: int,
    heading_path: list[str],
    model: str,
    budget: int,
    overlap_tokens: int,
    out: list[Chunk],
) -> None:
    if not text.strip():
        return

    # A segment that *opens* with a heading is owned by it; thread that heading into the
    # path so every sub-chunk keeps its chapter ancestry (an H1 at offset 0 must not be
    # dropped just because we split the block at the H2s inside it).
    headings = find_headings(text)
    own_offset = -1
    path = heading_path
    if headings and not text[: headings[0][0]].strip():
        own_offset = headings[0][0]
        path = [*heading_path, headings[0][2]]

    if count_text_tokens(text, model=model) <= budget:
        out.append(_make_chunk(text, abs_start, path))
        return

    sub = [h for h in headings if h[0] != own_offset]
    if not sub:
        # No finer heading to split on: paragraph/char fallback with overlap.
        _char_fallback(
            text,
            abs_start=abs_start,
            heading_path=path,
            model=model,
            budget=budget,
            overlap_tokens=overlap_tokens,
            out=out,
        )
        return

    min_level = min(level for _, level, _ in sub)
    cut_offsets = [off for off, level, _ in sub if level == min_level]
    boundaries = [0, *cut_offsets, len(text)]
    for i in range(len(boundaries) - 1):
        piece = text[boundaries[i] : boundaries[i + 1]]
        # Piece 0 re-contains the segment's own opening heading, so it recurses with the
        # *outer* path and re-derives ``own`` itself. Later pieces each open with a
        # sub-heading nested under ``own``, so they recurse with ``path`` (which includes
        # ``own``) and add their own sub-heading on the way down.
        child_path = heading_path if i == 0 else path
        _split_segment(
            piece,
            abs_start=abs_start + boundaries[i],
            heading_path=child_path,
            model=model,
            budget=budget,
            overlap_tokens=overlap_tokens,
            out=out,
        )


def _char_fallback(
    text: str,
    *,
    abs_start: int,
    heading_path: list[str],
    model: str,
    budget: int,
    overlap_tokens: int,
    out: list[Chunk],
) -> None:
    total_tokens = count_text_tokens(text, model=model)
    if total_tokens <= 0:
        out.append(_make_chunk(text, abs_start, heading_path))
        return
    chars_per_token = max(1.0, len(text) / total_tokens)
    window_chars = max(1, int(budget * chars_per_token * 0.9))
    overlap_chars = min(window_chars - 1, max(0, int(overlap_tokens * chars_per_token)))

    i = 0
    n = len(text)
    while i < n:
        end = min(n, i + window_chars)
        end = _snap_back_to_boundary(text, i, end)
        window = text[i:end]
        # Token estimate can undershoot on dense regions; shrink until it really fits.
        while end - i > 1 and count_text_tokens(window, model=model) > budget:
            end = _snap_back_to_boundary(text, i, i + int((end - i) * 0.85), force=True)
            window = text[i:end]
        out.append(_make_chunk(window, abs_start + i, heading_path))
        if end >= n:
            break
        i = max(end - overlap_chars, i + 1)


def _snap_back_to_boundary(text: str, start: int, end: int, *, force: bool = False) -> int:
    """Pull ``end`` back to the nearest paragraph (``\\n\\n``) or line boundary so a
    chunk does not cut mid-sentence. Only moves backward, so the window never grows
    above budget. ``force`` shrinks by at least one char even if no boundary is near."""
    if end >= len(text):
        return end
    window = text[start:end]
    para = window.rfind("\n\n")
    if para > 0:
        return start + para + 2
    line = window.rfind("\n")
    if line > 0:
        return start + line + 1
    return end - 1 if force else end


def _make_chunk(text: str, abs_start: int, heading_path: list[str]) -> Chunk:
    return Chunk(
        text=text,
        heading_path=list(heading_path),
        char_start=abs_start,
        char_end=abs_start + len(text),
        source_refs=SOURCE_REF_RE.findall(text),
    )


def find_headings(text: str) -> list[tuple[int, int, str]]:
    """Return ``(char_offset, level, title)`` for every ATX heading, skipping any line
    inside a fenced code block."""
    headings: list[tuple[int, int, str]] = []
    offset = 0
    in_fence = False
    fence_marker = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        fence = _FENCE_RE.match(stripped)
        if fence:
            marker = fence.group(1)[0]  # ` or ~
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
        elif not in_fence:
            match = _HEADING_RE.match(line.rstrip("\n"))
            if match:
                headings.append((offset, len(match.group(1)), match.group(2).strip()))
        offset += len(line)
    return headings
