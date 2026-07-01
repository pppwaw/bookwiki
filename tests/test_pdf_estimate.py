from __future__ import annotations

import zipfile
from pathlib import Path

from pypdf import PdfWriter

from bookwiki.scheduler.pdf_estimate import InputScan, scan


def _write_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_pptx(path: Path, slides: int) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for index in range(1, slides + 1):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<p:sld/>")


def test_scan_counts_pdf_pages_across_files(tmp_path: Path) -> None:
    _write_pdf(tmp_path / "a.pdf", 3)
    _write_pdf(tmp_path / "b.pdf", 2)
    result = scan(tmp_path)
    assert isinstance(result, InputScan)
    assert result.pdf_pages == 5


def test_scan_counts_pptx_slides(tmp_path: Path) -> None:
    _write_pptx(tmp_path / "deck.pptx", 4)
    result = scan(tmp_path)
    assert result.pptx_slides == 4


def test_scan_blank_pdf_has_no_images(tmp_path: Path) -> None:
    _write_pdf(tmp_path / "a.pdf", 2)
    result = scan(tmp_path)
    assert result.pdf_images == 0


def test_scan_records_unreadable_without_raising(tmp_path: Path) -> None:
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a real pdf")
    result = scan(tmp_path)
    assert [Path(p).name for p in result.unreadable] == ["broken.pdf"]
    assert result.pdf_pages == 0


def test_scan_empty_directory(tmp_path: Path) -> None:
    result = scan(tmp_path)
    assert result.pdf_pages == 0
    assert result.pptx_slides == 0
    assert result.pdf_images == 0
    assert result.unreadable == []
