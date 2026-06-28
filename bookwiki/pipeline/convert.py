from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from bookwiki.agents import (
    SourceLayoutRepairAgent,
    VisionCaptionAgent,
)
from bookwiki.convert.common import (
    source_id_from_stem,
)
from bookwiki.convert.mineru_client import convert_document_to_source
from bookwiki.convert.source_normalizer import (
    DecorativeImageThresholds,
    NormalizedSource,
    normalize_structured_source,
)
from bookwiki.convert.text_to_md import convert_text_to_md
from bookwiki.pipeline._shared import (
    _LOG,
    State,
    _book_figure_pattern,
    _cache_dir,
    _int_setting,
    _rel,
    _replace_book_figure,
    _stage_cache_hit,
    log_progress,
)
from bookwiki.scheduler.cache import CacheResult, run_with_cache
from bookwiki.scheduler.config import BookConfig
from bookwiki.utils.files import ensure_dir, read_json, write_json, write_text
from bookwiki.utils.hashing import sha256_file, sha256_text

CONVERT_ARTIFACT_VERSION = 2


def _source_file_metadata(path: Path, cfg: BookConfig, source_sha256: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": _rel(path, cfg.book_dir),
        "sha256": source_sha256,
        "size_bytes": stat.st_size,
    }


def _outputs_metadata(out_path: Path, cfg: BookConfig, body: str) -> dict[str, Any]:
    return {
        "markdown_path": _rel(out_path, cfg.book_dir),
        "markdown_sha256": sha256_text(body),
    }


def _attach_convert_metadata(
    manifest: dict[str, Any],
    *,
    source_path: Path,
    source_sha256: str,
    out_path: Path,
    body: str,
    cfg: BookConfig,
) -> dict[str, Any]:
    return {
        **manifest,
        "convert_artifact_version": CONVERT_ARTIFACT_VERSION,
        "source_file": _source_file_metadata(source_path, cfg, source_sha256),
        "outputs": _outputs_metadata(out_path, cfg, body),
    }


def _matching_convert_artifact(
    *,
    source_path: Path,
    source_sha256: str,
    out_path: Path,
    manifest_path: Path,
    cfg: BookConfig,
) -> bool:
    if not out_path.exists() or not manifest_path.exists():
        return False
    manifest = read_json(manifest_path, default={})
    if not isinstance(manifest, dict):
        return False
    source_file = manifest.get("source_file")
    outputs = manifest.get("outputs")
    if manifest.get("convert_artifact_version") != CONVERT_ARTIFACT_VERSION:
        return False
    if not isinstance(source_file, dict) or not isinstance(outputs, dict):
        return False
    if source_file.get("path") != _rel(source_path, cfg.book_dir):
        return False
    if source_file.get("sha256") != source_sha256:
        return False
    expected_markdown_sha256 = outputs.get("markdown_sha256")
    if not isinstance(expected_markdown_sha256, str) or not expected_markdown_sha256:
        return False
    try:
        body = out_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return sha256_text(body) == expected_markdown_sha256


async def convert_node(state: State, cfg: BookConfig) -> State:
    input_files = sorted(path for path in cfg.input_dir.iterdir() if path.is_file())
    if not input_files:
        msg = f"no input files found in {cfg.input_dir}"
        raise FileNotFoundError(msg)

    _LOG.info(
        "convert: input_files=%d dir=%s",
        len(input_files),
        cfg.input_dir,
    )
    out_dir = ensure_dir(cfg.work_dir / "sources_md")
    manifest_dir = ensure_dir(cfg.work_dir / "source_refs")
    outputs: list[str] = []
    manifests: list[str] = []
    reused = 0
    converted = 0
    total = len(input_files)
    for idx, path in enumerate(input_files, 1):
        source_id = source_id_from_stem(path.stem)
        out_path = out_dir / f"{source_id}.md"
        manifest_path = manifest_dir / f"{source_id}.json"
        source_sha256 = sha256_file(path)
        if _matching_convert_artifact(
            source_path=path,
            source_sha256=source_sha256,
            out_path=out_path,
            manifest_path=manifest_path,
            cfg=cfg,
        ):
            outputs.append(_rel(out_path, cfg.book_dir))
            manifests.append(_rel(manifest_path, cfg.book_dir))
            reused += 1
            continue
        suffix = path.suffix.lower()
        if suffix in {
            ".pdf",
            ".pptx",
            ".ppt",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".odt",
            ".odp",
            ".ods",
        }:
            log_progress("convert", idx, total, "source=%s via=mineru%s", path.name, suffix)
            parsed = convert_document_to_source(path, source_id=source_id)
            _materialize_mineru_assets(parsed, source_id, cfg)
            normalized = await _normalize_with_layout_repair(parsed, source_id, cfg)
            body = normalized.markdown
            manifest = normalized.manifest
        elif suffix in {".txt", ".md"}:
            log_progress("convert", idx, total, "source=%s via=text%s", path.name, suffix)
            body = convert_text_to_md(path, source_id=source_id)
            normalized = normalize_structured_source(raw_md=body, source_id=source_id)
            manifest = normalized.manifest
        else:
            msg = f"unsupported source file type: {path.name}"
            raise ValueError(msg)
        manifest = _attach_convert_metadata(
            manifest,
            source_path=path,
            source_sha256=source_sha256,
            out_path=out_path,
            body=body,
            cfg=cfg,
        )
        write_text(out_path, body)
        write_json(manifest_path, manifest)
        outputs.append(_rel(out_path, cfg.book_dir))
        manifests.append(_rel(manifest_path, cfg.book_dir))
        converted += 1
        log_progress(
            "convert", idx, total, "wrote source_id=%s markdown=%d bytes", source_id, len(body)
        )

    _LOG.info(
        "convert: done converted=%d reused=%d outputs=%d manifests=%d",
        converted,
        reused,
        len(outputs),
        len(manifests),
    )
    return {"sources_md": outputs, "source_ref_manifests": manifests}


async def caption_node(state: State, cfg: BookConfig) -> State:
    source_mds = [str(path) for path in state.get("sources_md") or []]
    manifests = [str(path) for path in state.get("source_ref_manifests") or []]
    if not source_mds:
        msg = "caption requires converted markdown; run convert first"
        raise FileNotFoundError(msg)
    if not manifests:
        msg = "caption requires source ref manifests; run convert first"
        raise FileNotFoundError(msg)

    md_by_source_id = {Path(path).stem: str(path) for path in source_mds}
    caption_results: list[dict[str, Any]] = []
    cache_results: list[CacheResult] = []
    caption_failures: list[str] = []
    settings = _vision_caption_settings(cfg)
    _LOG.info(
        "caption: mode=%s sources=%d manifests=%d max_images=%d max_concurrent=%d",
        settings["mode"],
        len(source_mds),
        len(manifests),
        settings["max_images"],
        settings["max_concurrent"],
    )

    cap_total = len(manifests)
    for cap_idx, manifest_rel in enumerate(manifests, 1):
        manifest_path = cfg.book_dir / manifest_rel
        if not manifest_path.exists():
            msg = f"caption source ref manifest not found: {manifest_path}"
            raise FileNotFoundError(msg)
        manifest = read_json(manifest_path, default={})
        if not isinstance(manifest, dict):
            msg = f"caption source ref manifest is not a JSON object: {manifest_path}"
            raise ValueError(msg)
        source_id = str(manifest.get("source_id") or Path(manifest_rel).stem)
        md_rel = md_by_source_id.get(source_id) or md_by_source_id.get(Path(manifest_rel).stem)
        md_path = cfg.book_dir / md_rel if md_rel else None
        if md_path is None or not md_path.exists():
            msg = f"caption converted markdown not found for {manifest_rel}"
            raise FileNotFoundError(msg)
        md_text = md_path.read_text(encoding="utf-8")
        warnings = [
            str(item)
            for item in manifest.get("vision_warnings", [])
            if isinstance(item, str) and item.strip()
        ]

        if settings["mode"] == "off":
            log_progress("caption", cap_idx, cap_total, "skip source_id=%s (mode=off)", source_id)
            continue

        normalized = NormalizedSource(markdown=md_text, manifest=manifest)
        candidates = _image_caption_candidates(normalized)[: settings["max_images"]]
        jobs = [_vision_caption_job(candidate, md_text) for candidate in candidates]
        log_progress(
            "caption",
            cap_idx,
            cap_total,
            "source_id=%s candidates=%d (max cap=%d)",
            source_id,
            len(jobs),
            settings["max_images"],
        )
        outcomes = await _run_vision_caption_jobs(
            jobs,
            cfg,
            max_concurrent=settings["max_concurrent"],
        )
        source_hits = 0
        source_misses = 0
        source_failures = 0
        for job, outcome in zip(jobs, outcomes, strict=False):
            candidate = job["candidate"]
            if isinstance(outcome, Exception):
                warning = f"vision caption failed for {candidate['block_id']}: {outcome}"
                warnings.append(warning)
                caption_failures.append(warning)
                source_failures += 1
                continue
            result = outcome

            block = _set_manifest_block_caption(
                manifest,
                candidate["block_id"],
                result.result.caption_md,
                model=cfg.model_for("vision"),
            )
            if block is None:
                warnings.append(
                    f"vision caption target block not found for {candidate['block_id']}"
                )
                continue
            if md_text:
                md_text, replaced = _replace_book_figure(md_text, block)
                if not replaced:
                    warnings.append(
                        f"vision caption markdown tag not found for {candidate['block_id']}"
                    )
            cache_results.append(result)
            if result.cache_hit:
                source_hits += 1
            else:
                source_misses += 1
            caption_results.append(
                {
                    "block_id": candidate["block_id"],
                    "source_ref": candidate["source_ref"],
                    "manifest": manifest_rel,
                    "cache_hit": result.cache_hit,
                }
            )

        if warnings:
            manifest["vision_warnings"] = warnings
        write_json(manifest_path, manifest)
        log_progress(
            "caption",
            cap_idx,
            cap_total,
            "source_id=%s done captions=%d hits=%d misses=%d failures=%d",
            source_id,
            source_hits + source_misses,
            source_hits,
            source_misses,
            source_failures,
        )
        # Deliberately do NOT write md_text back to sources_md. The convert artifact
        # (work/sources_md/*.md) must stay byte-identical to convert output so the convert
        # sha-idempotency gate (_matching_convert_artifact) keeps matching and MinerU output is
        # reused on rerun. Captions live only in the manifest (work/source_refs/*.json) and are
        # injected into the per-chapter sources at split time via _inject_book_figure_captions.

    if caption_failures:
        count = len(caption_failures)
        noun = "image" if count == 1 else "images"
        details = "; ".join(caption_failures[:3])
        if count > 3:
            details += f"; ... {count - 3} more"
        msg = f"caption failed for {count} {noun}: {details}"
        raise RuntimeError(msg)

    return {
        "sources_md": source_mds,
        "source_ref_manifests": manifests,
        "caption_results": caption_results,
        "cache_hit": not cache_results or _stage_cache_hit(cache_results),
    }


async def _normalize_with_layout_repair(
    parsed: dict[str, Any], source_id: str, cfg: BookConfig
) -> NormalizedSource:
    settings = _source_layout_repair_settings(cfg)
    decorative = _decorative_image_thresholds(cfg)
    normalized = normalize_structured_source(
        raw_md=str(parsed.get("markdown") or ""),
        source_id=source_id,
        content_list_v2=parsed.get("content_list_v2"),
        content_list=parsed.get("content_list"),
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
        asset_root=cfg.book_dir,
        decorative=decorative,
    )
    if settings["mode"] == "off" or not normalized.repair_candidates:
        return normalized

    result = await run_with_cache(
        SourceLayoutRepairAgent,
        {
            "source_id": source_id,
            "candidates": normalized.repair_candidates,
            "manifest": normalized.manifest,
        },
        model=cfg.model_for("source_layout_repair"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )
    patches = [
        patch.model_dump(mode="json")
        for patch in result.result.patches
        if patch.confidence >= settings["min_confidence"]
    ]
    if not patches:
        return normalized
    repaired = normalize_structured_source(
        raw_md=str(parsed.get("markdown") or ""),
        source_id=source_id,
        content_list_v2=parsed.get("content_list_v2"),
        content_list=parsed.get("content_list"),
        repair_patches=patches,
        min_confidence=settings["min_confidence"],
        max_candidates=settings["max_candidates"],
        asset_root=cfg.book_dir,
        decorative=decorative,
    )
    return repaired


def _materialize_mineru_assets(parsed: dict[str, Any], source_id: str, cfg: BookConfig) -> None:
    assets = [asset for asset in parsed.get("assets") or [] if isinstance(asset, dict)]
    if not assets:
        return
    asset_dir = ensure_dir(cfg.work_dir / "assets" / source_id)
    path_index: dict[str, str] = {}
    for index, asset in enumerate(assets, start=1):
        data = asset.get("data")
        if not isinstance(data, bytes):
            continue
        filename = _safe_asset_filename(str(asset.get("filename") or ""), index)
        out_path = asset_dir / filename
        out_path.write_bytes(data)
        rel_path = _rel(out_path, cfg.book_dir)
        archive_path = str(asset.get("archive_path") or filename).replace("\\", "/")
        for key in {archive_path, archive_path.lower(), Path(archive_path).name.lower()}:
            path_index[key] = rel_path
    if path_index:
        for value in (parsed.get("content_list_v2"), parsed.get("content_list")):
            _attach_asset_paths(value, path_index)


def _attach_asset_paths(value: Any, path_index: dict[str, str]) -> None:
    if isinstance(value, list):
        for item in value:
            _attach_asset_paths(item, path_index)
        return
    if not isinstance(value, dict):
        return
    block_type = str(value.get("type") or value.get("category") or "").lower()
    if block_type in {"image", "chart"} and not value.get("asset_path"):
        for raw in _asset_path_refs(value):
            rel_path = _asset_match(raw, path_index)
            if rel_path:
                value["asset_path"] = rel_path
                break
    for item in value.values():
        _attach_asset_paths(item, path_index)


def _asset_path_refs(value: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("img_path", "image_path", "path", "url"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            refs.append(raw)
    content = value.get("content")
    if isinstance(content, dict):
        image_source = content.get("image_source") or content.get("chart_source")
        if isinstance(image_source, dict):
            for key in ("img_path", "image_path", "path", "url"):
                raw = image_source.get(key)
                if isinstance(raw, str) and raw.strip():
                    refs.append(raw)
    return refs


def _asset_match(raw_path: str, path_index: dict[str, str]) -> str | None:
    normalized = raw_path.replace("\\", "/").lower().lstrip("/")
    if normalized in path_index:
        return path_index[normalized]
    basename = Path(normalized).name
    if basename in path_index:
        return path_index[basename]
    for key, rel_path in path_index.items():
        if key.endswith(normalized) or key.endswith("/" + basename):
            return rel_path
    return None


def _safe_asset_filename(filename: str, index: int) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(filename).name).strip(".-")
    if not clean:
        clean = f"asset-{index:03d}.png"
    return clean


def _vision_caption_job(candidate: dict[str, Any], md_text: str) -> dict[str, Any]:
    candidate_input = dict(candidate)
    section_window = _section_context_window_for_book_figure(md_text, candidate["block_id"])
    if section_window is not None:
        candidate_input["section_context"] = section_window["text"]
    return {
        "candidate": candidate,
        "input": candidate_input,
        "source_ref": str(candidate.get("source_ref") or ""),
    }


async def _run_vision_caption_jobs(
    jobs: list[dict[str, Any]],
    cfg: BookConfig,
    *,
    max_concurrent: int,
) -> list[CacheResult | Exception]:
    outcomes: list[CacheResult | Exception | None] = [None] * len(jobs)
    indexed_jobs = [{**job, "index": index} for index, job in enumerate(jobs)]
    groups = _caption_same_page_groups(indexed_jobs)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_group_jobs(group_jobs: list[dict[str, Any]]) -> list[CacheResult | Exception]:
        try:
            async with semaphore:
                result = await _run_vision_caption_group(group_jobs, cfg)
            return _caption_group_outcomes(group_jobs, result)
        except Exception as exc:  # noqa: BLE001 - captioning is best-effort enrichment
            return [exc for _job in group_jobs]

    async def run_group(group: dict[str, Any]) -> None:
        group_jobs = sorted(group["jobs"], key=lambda item: int(item["index"]))
        group_outcomes = await run_group_jobs(group_jobs)
        for job, outcome in zip(group_jobs, group_outcomes, strict=False):
            outcomes[int(job["index"])] = outcome

    await asyncio.gather(*(run_group(group) for group in groups))
    return [
        item if item is not None else RuntimeError("caption job did not run") for item in outcomes
    ]


def _caption_same_page_groups(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    first_index: dict[str, int] = {}
    for job in sorted(jobs, key=lambda item: int(item["index"])):
        source_ref = str(job.get("source_ref") or "")
        buckets.setdefault(source_ref, []).append(job)
        first_index.setdefault(source_ref, int(job["index"]))
    return [
        {
            "source_ref": source_ref,
            "jobs": group_jobs,
        }
        for source_ref, group_jobs in sorted(buckets.items(), key=lambda item: first_index[item[0]])
    ]


async def _run_vision_caption_group(jobs: list[dict[str, Any]], cfg: BookConfig) -> CacheResult:
    agent_input = _vision_caption_group_agent_input(jobs, cfg)
    return await run_with_cache(
        VisionCaptionAgent,
        agent_input,
        model=cfg.model_for("vision"),
        cache_dir=_cache_dir(cfg),
        runtime=cfg.llm_runtime,
    )


def _vision_caption_group_agent_input(
    jobs: list[dict[str, Any]], cfg: BookConfig
) -> dict[str, Any]:
    images = [_vision_caption_agent_input(job["input"], cfg) for job in jobs]
    source_ref = str(jobs[0].get("source_ref") or "") if jobs else ""
    return {"source_ref": source_ref, "images": images}


def _caption_group_outcomes(
    jobs: list[dict[str, Any]], batch_result: CacheResult
) -> list[CacheResult]:
    captions = getattr(batch_result.result, "captions", [])
    by_block_id = {str(item.block_id): item for item in captions}
    outcomes: list[CacheResult] = []
    for job in jobs:
        candidate = job["candidate"]
        block_id = str(candidate["block_id"])
        item = by_block_id.get(block_id)
        if item is None:
            msg = f"batch caption missing result for {block_id}"
            raise RuntimeError(msg)
        outcomes.append(
            CacheResult(
                result=item,
                cache_hit=batch_result.cache_hit,
                key=batch_result.key,
                path=batch_result.path,
            )
        )
    return outcomes


def _set_manifest_block_caption(
    manifest: dict[str, Any], block_id: str, caption: str, *, model: str
) -> dict[str, Any] | None:
    block = _manifest_block(manifest, block_id)
    if block is None:
        return None
    block["caption"] = caption
    block["caption_source"] = "vision"
    block["caption_model"] = model
    return block


def _manifest_block(manifest: dict[str, Any], block_id: str) -> dict[str, Any] | None:
    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks", []):
            if isinstance(block, dict) and str(block.get("block_id") or "") == block_id:
                return block
    return None


def _section_context_window_for_book_figure(markdown: str, block_id: str) -> dict[str, Any] | None:
    match = _book_figure_pattern(block_id).search(markdown)
    if not match:
        return None
    start = 0
    end = len(markdown)
    for heading in re.finditer(r"(?m)^#{1,6}\s+\S.*$", markdown):
        if heading.start() <= match.start():
            start = heading.start()
            continue
        end = heading.start()
        break
    return {
        "text": markdown[start:end].strip(),
        "span": (start, end),
    }


def _image_caption_candidates(normalized: NormalizedSource) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for page in normalized.manifest.get("pages", []):
        blocks = page.get("blocks", []) if isinstance(page, dict) else []
        nearby_text = " ".join(
            str(block.get("text_preview") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") not in {"image", "chart"}
        )
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in {"image", "chart"}:
                continue
            if not block.get("asset_path"):
                continue
            if str(block.get("caption_source") or "").lower() == "vision":
                continue
            existing_caption = str(block.get("caption") or "").strip()
            candidates.append(
                {
                    "block_id": str(block.get("block_id") or ""),
                    "source_ref": str(block.get("page_ref") or page.get("source_ref") or ""),
                    "asset_path": str(block.get("asset_path") or ""),
                    "existing_caption": existing_caption,
                    "nearby_text": nearby_text,
                    "bbox": block.get("bbox"),
                }
            )
    return candidates


def _vision_caption_agent_input(candidate: dict[str, Any], cfg: BookConfig) -> dict[str, Any]:
    agent_input = dict(candidate)
    image_path = Path(str(candidate.get("asset_path") or ""))
    if not image_path.is_absolute():
        image_path = cfg.book_dir / image_path
    agent_input["asset_full_path"] = str(image_path)
    if image_path.is_file():
        agent_input["asset_sha256"] = sha256_file(image_path)
    return agent_input


def _vision_caption_settings(cfg: BookConfig) -> dict[str, Any]:
    raw = cfg.generation.get("visionCaption")
    settings = raw if isinstance(raw, dict) else {}
    mode = str(settings.get("mode", "auto")).lower()
    if mode not in {"auto", "off"}:
        mode = "auto"
    return {
        "mode": mode,
        "max_images": _int_setting(settings.get("maxImagesPerSource"), 20),
        "max_concurrent": _int_setting(settings.get("maxConcurrent"), 10),
    }


def _source_layout_repair_settings(cfg: BookConfig) -> dict[str, Any]:
    raw = cfg.generation.get("sourceLayoutRepair")
    settings = raw if isinstance(raw, dict) else {}
    mode = str(settings.get("mode", "auto")).lower()
    if mode not in {"auto", "off"}:
        mode = "auto"
    return {
        "mode": mode,
        "min_confidence": _float_setting(settings.get("minConfidence"), 0.85),
        "max_candidates": _int_setting(settings.get("maxCandidatesPerSource"), 20),
    }


def _decorative_image_thresholds(cfg: BookConfig) -> DecorativeImageThresholds | None:
    """Build the decorative-image size floors from config, or ``None`` to disable.

    Set ``generation.decorativeImageFilter.mode = "off"`` to keep every extracted image
    block. The size keys override individual defaults of ``DecorativeImageThresholds``.
    """
    raw = cfg.generation.get("decorativeImageFilter")
    settings = raw if isinstance(raw, dict) else {}
    if str(settings.get("mode", "auto")).lower() == "off":
        return None
    defaults = DecorativeImageThresholds()
    return DecorativeImageThresholds(
        min_pixel_side=_int_setting(settings.get("minPixelSide"), defaults.min_pixel_side),
        min_pixel_area=_int_setting(settings.get("minPixelArea"), defaults.min_pixel_area),
        min_bbox_side=_float_setting(settings.get("minBboxSide"), defaults.min_bbox_side),
        min_bbox_area=_float_setting(settings.get("minBboxArea"), defaults.min_bbox_area),
    )


def _float_setting(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


__all__ = [
    "CONVERT_ARTIFACT_VERSION",
    "_source_file_metadata",
    "_outputs_metadata",
    "_attach_convert_metadata",
    "_matching_convert_artifact",
    "convert_node",
    "caption_node",
    "_normalize_with_layout_repair",
    "_materialize_mineru_assets",
    "_attach_asset_paths",
    "_asset_path_refs",
    "_asset_match",
    "_safe_asset_filename",
    "_vision_caption_job",
    "_run_vision_caption_jobs",
    "_caption_same_page_groups",
    "_run_vision_caption_group",
    "_vision_caption_group_agent_input",
    "_caption_group_outcomes",
    "_set_manifest_block_caption",
    "_manifest_block",
    "_section_context_window_for_book_figure",
    "_image_caption_candidates",
    "_vision_caption_agent_input",
    "_vision_caption_settings",
    "_source_layout_repair_settings",
    "_decorative_image_thresholds",
    "_float_setting",
]
