from __future__ import annotations

import json
from pathlib import Path

import yaml
from pypdf import PdfWriter

from bookwiki.scheduler.config import default_config
from bookwiki.scheduler.resume import dry_run_report, resolve_scale


def _book_with_pdf(tmp_path: Path, pages: int):
    book = tmp_path / "b"
    (book / "input").mkdir(parents=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    with (book / "input" / "src.pdf").open("wb") as handle:
        writer.write(handle)
    return default_config(book)


def _write_structure(cfg, chapters) -> None:
    structure_dir = cfg.work_dir / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "approved-structure.yaml").write_text(
        yaml.safe_dump({"chapters": chapters}), encoding="utf-8"
    )


def test_resolve_scale_no_structure_uses_input_content(tmp_path: Path) -> None:
    cfg = _book_with_pdf(tmp_path, 43)  # 43 pages / 21.6 ≈ 2 sections
    scale, basis = resolve_scale(cfg)
    assert scale.sections == 2
    assert "input" in basis.lower()


def test_resolve_scale_with_structure_uses_real_section_count(tmp_path: Path) -> None:
    cfg = _book_with_pdf(tmp_path, 43)
    _write_structure(cfg, [{"sections": [1, 2, 3]}, {"sections": [4, 5, 6, 7]}])
    scale, basis = resolve_scale(cfg)
    assert scale.sections == 7
    assert "structure" in basis.lower()


def test_resolve_scale_uses_concept_graph_when_present(tmp_path: Path) -> None:
    cfg = _book_with_pdf(tmp_path, 43)
    _write_structure(cfg, [{"sections": list(range(7))}])
    (cfg.work_dir / "concept-graph.json").write_text(
        json.dumps({"nodes": list(range(20))}), encoding="utf-8"
    )
    scale, _ = resolve_scale(cfg)
    assert scale.concepts == 20


def test_dry_run_report_contains_estimate_and_graph(tmp_path: Path) -> None:
    cfg = _book_with_pdf(tmp_path, 43)
    report = dry_run_report(cfg)
    assert "graph TD" in report
    assert "Estimated cost CNY" in report
