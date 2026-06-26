from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bookwiki.convert.common import SOURCE_REF_RE, clean_markdown

_LOG = logging.getLogger(__name__)

NOISE_TYPES = {"header", "footer", "page_number"}
TABLE_TYPES = {"table", "chart"}
CAPTION_TYPES = {"table_caption", "image_caption", "chart_caption"}
TEXT_KEYS = (
    "content",
    "text",
    "md",
    "markdown",
    "html",
    "table_body",
    "code_body",
    "latex",
)
CONTENT_DICT_TEXT_KEYS = (
    "paragraph_content",
    "title_content",
    "math_content",
    "html",
    "list_items",
    "content",
    "table_caption",
    "table_footnote",
    "image_caption",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
)
MATH_BLOCK_TYPES = {"equation", "interline_equation", "equation_interline"}
INLINE_MATH_KINDS = {"inline", "inline_equation"}
DISPLAY_MATH_KINDS = {
    "display",
    "interline",
    "block",
    "equation",
    "interline_equation",
    "equation_interline",
}
MATH_SPAN_TYPES = {"inline_equation", "interline_equation"}
ALLOWED_REPAIR_ACTIONS = {
    "link_table_parts",
    "attach_caption",
    "promote_heading",
    "demote_repeating_header_footer",
}


@dataclass
class SourceBlock:
    block_id: str
    page_ref: str
    page_idx: int
    block_index: int
    type: str
    text: str
    bbox: list[float | int] | None = None
    asset_path: str | None = None
    caption: str | None = None
    attached_to: str | None = None

    @property
    def text_preview(self) -> str:
        return re.sub(r"\s+", " ", self.text).strip()[:240]

    def to_manifest(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "block_id": self.block_id,
            "page_ref": self.page_ref,
            "page_idx": self.page_idx,
            "block_index": self.block_index,
            "type": self.type,
            "text_preview": self.text_preview,
        }
        if self.bbox is not None:
            payload["bbox"] = self.bbox
        if self.asset_path:
            payload["asset_path"] = self.asset_path
        if self.caption:
            payload["caption"] = self.caption
        if self.attached_to:
            payload["attached_to"] = self.attached_to
        return payload


@dataclass
class SourcePage:
    page_idx: int
    source_ref: str
    blocks: list[SourceBlock] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "page_idx": self.page_idx,
            "page_number": self.page_idx + 1,
            "source_ref": self.source_ref,
            "blocks": [block.to_manifest() for block in self.blocks],
        }


@dataclass
class NormalizedSource:
    markdown: str
    manifest: dict[str, Any]
    repair_candidates: list[dict[str, Any]] = field(default_factory=list)


def normalize_structured_source(
    *,
    raw_md: str,
    source_id: str,
    content_list_v2: Any | None = None,
    content_list: Any | None = None,
    repair_patches: list[dict[str, Any]] | None = None,
    block_overrides: dict[str, dict[str, Any]] | None = None,
    min_confidence: float = 0.85,
    max_candidates: int = 20,
    asset_root: Path | None = None,
    decorative: DecorativeImageThresholds | None = None,
) -> NormalizedSource:
    dropped: list[dict[str, Any]] = []
    ctx = _FilterContext(asset_root=asset_root, decorative=decorative, dropped=dropped)
    pages = _pages_from_content_list_v2(source_id, content_list_v2, block_overrides, ctx)
    if not pages:
        pages = _pages_from_content_list(source_id, content_list, block_overrides, ctx)
    if not pages:
        pages = _fallback_pages(source_id, raw_md)

    repair_candidates = _repair_candidates(pages, max_candidates=max_candidates)
    logical_tables: list[dict[str, Any]] = []
    warnings: list[str] = []
    if repair_patches:
        _apply_repair_patches(
            pages=pages,
            patches=repair_patches,
            logical_tables=logical_tables,
            warnings=warnings,
        )

    if dropped:
        _LOG.info(
            "normalize: source_id=%s dropped %d decorative image/chart block(s): %s",
            source_id,
            len(dropped),
            ", ".join(f"{item['block_id']}({item['reason']})" for item in dropped[:5])
            + (f", +{len(dropped) - 5} more" if len(dropped) > 5 else ""),
        )

    manifest = {
        "source_id": source_id,
        "ref_granularity": "page",
        "pages": [page.to_manifest() for page in pages],
        "logical_tables": logical_tables,
        "repair_candidates": repair_candidates,
        "repair_warnings": warnings,
        "repair_min_confidence": min_confidence,
    }
    if dropped:
        manifest["dropped_decorative_blocks"] = dropped
    return NormalizedSource(
        markdown=_render_markdown(source_id, pages, logical_tables),
        manifest=manifest,
        repair_candidates=repair_candidates,
    )


def _pages_from_content_list_v2(
    source_id: str,
    value: Any,
    block_overrides: dict[str, dict[str, Any]] | None = None,
    ctx: _FilterContext | None = None,
) -> list[SourcePage]:
    if not isinstance(value, list):
        return []

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(value):
        if isinstance(item, list):
            grouped[index] = [block for block in item if isinstance(block, dict)]
            continue
        if not isinstance(item, dict):
            continue
        if _looks_like_page_container(item):
            page_idx = _page_idx(item, index)
            grouped.setdefault(page_idx, []).extend(_container_items(item))
        elif "page_idx" in item:
            grouped.setdefault(_page_idx(item, 0), []).append(item)

    return _pages_from_grouped_blocks(source_id, grouped, block_overrides, ctx)


def _pages_from_content_list(
    source_id: str,
    value: Any,
    block_overrides: dict[str, dict[str, Any]] | None = None,
    ctx: _FilterContext | None = None,
) -> list[SourcePage]:
    if not isinstance(value, list):
        return []
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        grouped.setdefault(_page_idx(item, 0), []).append(item)
    return _pages_from_grouped_blocks(source_id, grouped, block_overrides, ctx)


def _looks_like_page_container(item: dict[str, Any]) -> bool:
    if any(isinstance(item.get(key), list) for key in ("items", "blocks", "contents", "children")):
        return True
    return "page_idx" in item and "type" not in item


def _container_items(item: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "blocks", "contents", "children"):
        value = item.get(key)
        if isinstance(value, list):
            return [block for block in value if isinstance(block, dict)]
    value = item.get("content")
    if isinstance(value, list):
        return [block for block in value if isinstance(block, dict)]
    return []


def _pages_from_grouped_blocks(
    source_id: str,
    grouped: dict[int, list[dict[str, Any]]],
    block_overrides: dict[str, dict[str, Any]] | None = None,
    ctx: _FilterContext | None = None,
) -> list[SourcePage]:
    pages: list[SourcePage] = []
    for page_idx in sorted(grouped):
        source_ref = _page_ref(source_id, page_idx)
        blocks: list[SourceBlock] = []
        for block_index, raw in enumerate(grouped[page_idx], start=1):
            block_type = _block_type(raw)
            text = _block_text(raw, block_type=block_type)
            block_id = f"{source_ref}-b{block_index:03d}"
            overrides = block_overrides.get(block_id, {}) if block_overrides else {}
            asset_path = _string_override(overrides, "asset_path") or _asset_path(raw)
            caption = _string_override(overrides, "caption") or _caption(raw)
            bbox = _bbox(raw)
            if not text and block_type not in {"image", "table", "chart"}:
                continue
            if (
                ctx
                and ctx.decorative is not None
                and block_type in {"image", "chart"}
                and asset_path
            ):
                reason = _decorative_image_reason(
                    asset_path=asset_path,
                    bbox=bbox,
                    asset_root=ctx.asset_root,
                    thresholds=ctx.decorative,
                )
                if reason is not None:
                    ctx.dropped.append(
                        {
                            "block_id": block_id,
                            "page_ref": source_ref,
                            "type": block_type,
                            "asset_path": asset_path,
                            "reason": reason,
                        }
                    )
                    continue
            blocks.append(
                SourceBlock(
                    block_id=block_id,
                    page_ref=source_ref,
                    page_idx=page_idx,
                    block_index=block_index,
                    type=block_type,
                    text=text,
                    bbox=bbox,
                    asset_path=asset_path,
                    caption=caption,
                )
            )
        pages.append(SourcePage(page_idx=page_idx, source_ref=source_ref, blocks=blocks))
    return pages


def _fallback_pages(source_id: str, raw_md: str) -> list[SourcePage]:
    cleaned = clean_markdown(raw_md)
    if not cleaned:
        cleaned = "No extractable text was returned by MinerU."

    matches = list(SOURCE_REF_RE.finditer(cleaned))
    if matches:
        pages: list[SourcePage] = []
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
            source_ref = match.group(1)
            body = cleaned[match.end() : next_start].strip()
            block = SourceBlock(
                block_id=f"{source_ref}-b001",
                page_ref=source_ref,
                page_idx=index,
                block_index=1,
                type="text",
                text=body,
            )
            pages.append(SourcePage(page_idx=index, source_ref=source_ref, blocks=[block]))
        return pages

    chunks = [chunk for chunk in raw_md.split("\x0c") if chunk.strip()] if "\x0c" in raw_md else []
    if not chunks:
        chunks = [cleaned]
    pages = []
    for page_idx, chunk in enumerate(chunks):
        source_ref = _page_ref(source_id, page_idx)
        pages.append(
            SourcePage(
                page_idx=page_idx,
                source_ref=source_ref,
                blocks=[
                    SourceBlock(
                        block_id=f"{source_ref}-b001",
                        page_ref=source_ref,
                        page_idx=page_idx,
                        block_index=1,
                        type="text",
                        text=clean_markdown(chunk),
                    )
                ],
            )
        )
    return pages


def _repair_candidates(pages: list[SourcePage], *, max_candidates: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for left, right in zip(pages, pages[1:], strict=False):
        left_block = _last_meaningful_block(left)
        right_block = _first_meaningful_block(right)
        if not left_block or not right_block:
            continue
        if left_block.type in TABLE_TYPES and right_block.type in TABLE_TYPES:
            candidates.append(
                {
                    "candidate_id": f"{left_block.block_id}-to-{right_block.block_id}",
                    "kind": "table_continuation",
                    "confidence": 0.65,
                    "source_block_id": left_block.block_id,
                    "target_block_id": right_block.block_id,
                    "page_refs": [left.source_ref, right.source_ref],
                    "reason": "adjacent pages end and begin with table-like blocks",
                }
            )
        if len(candidates) >= max_candidates:
            break
    return candidates


def _apply_repair_patches(
    *,
    pages: list[SourcePage],
    patches: list[dict[str, Any]],
    logical_tables: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    blocks = {block.block_id: block for page in pages for block in page.blocks}
    for patch in patches:
        action = str(patch.get("action") or "")
        if action not in ALLOWED_REPAIR_ACTIONS:
            warnings.append(f"unsupported repair action {action!r}")
            continue
        source_id = str(patch.get("source_block_id") or "")
        target_id = str(patch.get("target_block_id") or "")
        source_block = blocks.get(source_id)
        target_block = blocks.get(target_id) if target_id else None
        if source_block is None:
            warnings.append(f"unknown block id {source_id!r} for action {action}")
            continue
        if action in {"link_table_parts", "attach_caption"} and target_block is None:
            warnings.append(f"unknown block id {target_id!r} for action {action}")
            continue

        if action == "link_table_parts":
            assert target_block is not None
            table_id = (
                f"{_source_id_from_page_ref(source_block.page_ref)}-table-"
                f"{len(logical_tables) + 1:03d}"
            )
            logical_tables.append(
                {
                    "table_id": table_id,
                    "canonical_ref": source_block.page_ref,
                    "confidence": _float(patch.get("confidence"), 0.0),
                    "reason": str(patch.get("reason") or ""),
                    "parts": [
                        {
                            "block_id": source_block.block_id,
                            "page_ref": source_block.page_ref,
                            "role": "start",
                        },
                        {
                            "block_id": target_block.block_id,
                            "page_ref": target_block.page_ref,
                            "role": "continuation",
                        },
                    ],
                }
            )
        elif action == "attach_caption":
            assert target_block is not None
            source_block.attached_to = target_block.block_id
        elif action == "promote_heading":
            source_block.type = "title"
        elif action == "demote_repeating_header_footer":
            source_block.type = "header"


def _render_markdown(
    source_id: str, pages: list[SourcePage], logical_tables: list[dict[str, Any]]
) -> str:
    table_part_index: dict[str, tuple[str, str]] = {}
    for table in logical_tables:
        table_id = str(table.get("table_id") or "")
        for part in table.get("parts", []):
            if isinstance(part, dict):
                table_part_index[str(part.get("block_id") or "")] = (
                    table_id,
                    str(part.get("role") or "part"),
                )

    blocks = [f"# {source_id}"]
    for page in pages:
        page_lines = [f"<!-- source_ref: {page.source_ref} -->"]
        for block in page.blocks:
            if block.type in NOISE_TYPES:
                continue
            if block.block_id in table_part_index:
                table_id, role = table_part_index[block.block_id]
                page_lines.extend(
                    [
                        "",
                        f"<!-- logical_table: {table_id} part: {role} block: {block.block_id} -->",
                    ]
                )
            rendered = _render_block(block)
            if rendered:
                page_lines.extend(["", rendered])
        blocks.append("\n".join(page_lines).strip())
    return "\n\n".join(blocks).strip() + "\n"


def _render_block(block: SourceBlock) -> str:
    text = clean_markdown(block.text)
    if block.type in {"image", "chart"}:
        return _render_figure(block, text)
    if not text:
        return ""
    if block.type == "title":
        return f"### {text.lstrip('#').strip()}"
    if block.type in MATH_BLOCK_TYPES:
        return _display_math(text)
    return text


def _render_figure(block: SourceBlock, text: str) -> str:
    caption = block.caption or text
    if not block.asset_path and not caption:
        return ""
    props = [
        ("id", block.block_id),
        ("sourceRef", block.page_ref),
    ]
    if block.asset_path:
        props.append(("src", _public_asset_path(block.asset_path)))
    if caption:
        props.append(("caption", caption))
    return "<BookFigure " + " ".join(_jsx_attr(name, value) for name, value in props) + " />"


def _jsx_attr(name: str, value: str) -> str:
    return f'{name}="{html.escape(str(value), quote=True)}"'


def _public_asset_path(asset_path: str) -> str:
    normalized = asset_path.replace("\\", "/")
    prefix = "work/assets/"
    if normalized.startswith(prefix):
        return "/bookwiki-assets/" + normalized.removeprefix(prefix)
    return normalized


def _last_meaningful_block(page: SourcePage) -> SourceBlock | None:
    for block in reversed(page.blocks):
        if block.type not in NOISE_TYPES and block.text_preview:
            return block
    return None


def _first_meaningful_block(page: SourcePage) -> SourceBlock | None:
    for block in page.blocks:
        if block.type not in NOISE_TYPES and block.text_preview:
            return block
    return None


def _block_type(raw: dict[str, Any]) -> str:
    value = raw.get("type") or raw.get("category") or "text"
    return str(value).strip().lower() or "text"


def _block_text(raw: dict[str, Any], *, block_type: str | None = None) -> str:
    block_type = block_type or _block_type(raw)
    if isinstance(raw.get("list_items"), list):
        return "\n".join(f"- {item}" for item in raw["list_items"])
    for key in TEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            return clean_markdown(value)
        if isinstance(value, dict):
            text = _content_dict_text(value, block_type=block_type)
            if text:
                return text
        if isinstance(value, list):
            text = _content_list_text(value, block_type=block_type)
            if text:
                return text
    lines = raw.get("lines")
    if isinstance(lines, list):
        return clean_markdown("\n".join(_line_text(line) for line in lines))
    blocks = raw.get("blocks")
    if isinstance(blocks, list):
        return clean_markdown(
            "\n".join(_block_text(block) for block in blocks if isinstance(block, dict))
        )
    return ""


def _asset_path(raw: dict[str, Any]) -> str | None:
    for key in ("asset_path", "image_path", "img_path", "path", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\\", "/")
    return None


def _caption(raw: dict[str, Any]) -> str | None:
    for key in ("caption", "image_caption", "chart_caption", "table_caption"):
        value = raw.get(key)
        text = _content_value_text(value)
        if text:
            return clean_markdown(text)
    content = raw.get("content")
    if isinstance(content, dict):
        for key in ("image_caption", "chart_caption", "table_caption"):
            text = _content_value_text(content.get(key))
            if text:
                return clean_markdown(text)
    return None


def _string_override(overrides: dict[str, Any], key: str) -> str | None:
    value = overrides.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip().replace("\\", "/") if key.endswith("path") else value.strip()
    return None


def _content_dict_text(value: dict[str, Any], *, block_type: str = "") -> str:
    span_type = str(value.get("type") or "").strip().lower()
    if span_type in MATH_SPAN_TYPES:
        text = _content_value_text(_primary_content_value(value), block_type=block_type)
        return _format_math(text, math_type=span_type, block_type=block_type)

    parts: list[str] = []
    for key in CONTENT_DICT_TEXT_KEYS:
        item = value.get(key)
        text = _content_value_text(item, block_type=block_type)
        if text:
            if key == "math_content" and block_type not in MATH_BLOCK_TYPES:
                text = _format_math(
                    text,
                    math_type=str(value.get("math_type") or ""),
                    block_type=block_type,
                )
            parts.append(text)
    return clean_markdown("\n".join(parts))


def _content_list_text(value: list[Any], *, block_type: str = "") -> str:
    return clean_markdown(
        "".join(_content_value_text(item, block_type=block_type) for item in value)
    )


def _content_value_text(value: Any, *, block_type: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return _content_list_text(value, block_type=block_type)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "latex", "html"):
            nested = value.get(key)
            if nested is not None:
                text = _content_value_text(nested, block_type=block_type)
                span_type = str(value.get("type") or "").strip().lower()
                if span_type in MATH_SPAN_TYPES:
                    return _format_math(text, math_type=span_type, block_type=block_type)
                return text
        return _content_dict_text(value, block_type=block_type)
    return ""


def _primary_content_value(value: dict[str, Any]) -> Any:
    for key in ("content", "text", "latex", "value"):
        if key in value:
            return value[key]
    return ""


def _line_text(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    spans = raw.get("spans")
    if isinstance(spans, list):
        return "".join(_span_text(span) for span in spans if isinstance(span, dict))
    return str(raw.get("content") or raw.get("text") or "")


def _span_text(span: dict[str, Any]) -> str:
    text = str(span.get("content") or "")
    span_type = str(span.get("type") or "").strip().lower()
    if span_type in MATH_SPAN_TYPES:
        return _format_math(text, math_type=span_type, block_type="")
    return text


def _format_math(text: str, *, math_type: str = "", block_type: str = "") -> str:
    kind = math_type.strip().lower()
    if kind in INLINE_MATH_KINDS:
        return _inline_math(text)
    if kind in DISPLAY_MATH_KINDS or block_type in MATH_BLOCK_TYPES:
        return _display_math(text)
    return _display_math(text) if "\n" in clean_markdown(text) else _inline_math(text)


def _inline_math(text: str) -> str:
    body = _strip_math_delimiters(text)
    return f"${body}$" if body else ""


def _display_math(text: str) -> str:
    body = _strip_math_delimiters(text)
    return f"$$\n{body}\n$$" if body else ""


def _strip_math_delimiters(text: str) -> str:
    body = clean_markdown(text)
    if body.startswith("$$") and body.endswith("$$"):
        return clean_markdown(body[2:-2])
    if body.startswith(r"\[") and body.endswith(r"\]"):
        return clean_markdown(body[2:-2])
    if body.startswith("$") and body.endswith("$"):
        return clean_markdown(body[1:-1])
    if body.startswith(r"\(") and body.endswith(r"\)"):
        return clean_markdown(body[2:-2])
    return body


def _bbox(raw: dict[str, Any]) -> list[float | int] | None:
    value = raw.get("bbox")
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(item, int | float) for item in value):
        return None
    return value


@dataclass(frozen=True)
class DecorativeImageThresholds:
    """Size floors below which an extracted ``image``/``chart`` block is treated as a
    decorative glyph (arrow, icon, rule, logo) rather than a real figure and dropped.

    The pixel dimensions of the extracted asset are the primary signal; the source
    ``bbox`` is a fallback for when the asset file cannot be opened (e.g. Pillow missing
    or the asset is not on disk yet).
    """

    min_pixel_side: int = 180
    min_pixel_area: int = 30_000
    min_bbox_side: float = 120.0
    min_bbox_area: float = 20_000.0


@dataclass
class _FilterContext:
    """Carries decorative-image filtering inputs/outputs through the page builders."""

    asset_root: Path | None = None
    decorative: DecorativeImageThresholds | None = None
    dropped: list[dict[str, Any]] = field(default_factory=list)


def _image_pixel_size(asset_path: str | None, asset_root: Path | None) -> tuple[int, int] | None:
    if not asset_path:
        return None
    path = Path(asset_path)
    if not path.is_absolute() and asset_root is not None:
        path = asset_root / path
    try:
        from PIL import Image  # lazy: Pillow ships with the runtime extras (via matplotlib)
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _decorative_image_reason(
    *,
    asset_path: str | None,
    bbox: list[float | int] | None,
    asset_root: Path | None,
    thresholds: DecorativeImageThresholds,
) -> str | None:
    """Return a human-readable reason when an image/chart block is too small to be a
    real figure, else ``None``. Pixel dimensions win; bbox is the fallback. When no size
    signal is available the block is kept rather than dropped blindly."""
    pixels = _image_pixel_size(asset_path, asset_root)
    if pixels is not None:
        width, height = pixels
        if (
            min(width, height) < thresholds.min_pixel_side
            or width * height < thresholds.min_pixel_area
        ):
            return f"pixels={width}x{height}"
        return None
    if bbox is not None and len(bbox) == 4:
        bw = abs(bbox[2] - bbox[0])
        bh = abs(bbox[3] - bbox[1])
        if min(bw, bh) < thresholds.min_bbox_side or bw * bh < thresholds.min_bbox_area:
            return f"bbox={bw:.0f}x{bh:.0f}"
        return None
    return None


def _page_idx(raw: dict[str, Any], default: int) -> int:
    try:
        parsed = int(raw.get("page_idx", default))
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _page_ref(source_id: str, page_idx: int) -> str:
    return f"{source_id}-p{page_idx + 1:03d}"


def _source_id_from_page_ref(page_ref: str) -> str:
    return re.sub(r"-p\d+$", "", page_ref)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
