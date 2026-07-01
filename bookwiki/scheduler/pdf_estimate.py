from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from bookwiki.utils.logging import get_logger

_LOG = get_logger(__name__)

# Formats the pipeline's ``convert`` stage accepts and we can size cheaply offline.
_PDF_SUFFIXES = {".pdf"}
_PPTX_SUFFIXES = {".pptx"}


@dataclass(frozen=True)
class InputScan:
    """Structural counts of a book's ``input/`` directory, for offline cost estimate.

    ``pdf_pages`` + ``pptx_slides`` drive the section-count estimate (see
    ``scheduler.dry_run``); ``pdf_images`` feeds the caption estimate. ``unreadable``
    lists files that could not be parsed (encrypted / corrupt) so ``--dry-run`` can
    surface them instead of silently under-counting.
    """

    pdf_pages: int = 0
    pptx_slides: int = 0
    pdf_images: int = 0
    unreadable: list[str] = field(default_factory=list)


def scan(input_dir: Path) -> InputScan:
    """Count PDF pages/images and PPTX slides under ``input_dir`` (non-recursive).

    Best-effort: a file that cannot be read is recorded in ``unreadable`` and skipped,
    never raised — a single corrupt source must not abort the whole estimate.
    """
    pdf_pages = 0
    pdf_images = 0
    pptx_slides = 0
    unreadable: list[str] = []

    if not input_dir.is_dir():
        return InputScan(unreadable=unreadable)

    for path in sorted(input_dir.iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in _PDF_SUFFIXES:
                reader = PdfReader(str(path))
                pdf_pages += len(reader.pages)
                pdf_images += sum(len(getattr(page, "images", []) or []) for page in reader.pages)
            elif suffix in _PPTX_SUFFIXES:
                with zipfile.ZipFile(path) as archive:
                    pptx_slides += sum(
                        1
                        for name in archive.namelist()
                        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    )
        except Exception as error:  # noqa: BLE001 - one bad file must not break --dry-run
            _LOG.warning("dry-run: cannot read input %s (%s)", path.name, error)
            unreadable.append(str(path))

    return InputScan(
        pdf_pages=pdf_pages,
        pptx_slides=pptx_slides,
        pdf_images=pdf_images,
        unreadable=unreadable,
    )
