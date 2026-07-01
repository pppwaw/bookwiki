from __future__ import annotations

from dataclasses import dataclass

from bookwiki.scheduler.pdf_estimate import InputScan

# Offline cost model for ``--dry-run`` only; the real per-call cost is computed by the
# runtime from registered prices (see scheduler.llm). All figures are the "ideal single
# clean pass" (repair excluded), calibrated from real ``llm_usage`` of full calculus +
# circuits runs (see each book's ``work/logs/run-manifest.json``).
#
# Driver dimensions (unit cost / unit tokens):
#   per section  — generate + build_skeleton
#   per concept  — concept_pages
#   per image    — caption (per captioned image)
#   one-shot     — structure + split + index (weakly size-dependent, modelled flat)
#
# Confidence (calculus vs circuits):
#   strong (< 8%): per_section_generate, per_image_caption, and the totals
#   weak (cross-book spread / single sample): per_concept, the scale ratios below
PER_SECTION_GENERATE = 0.898002  # calculus run5 generate_chapter / 40
PER_SECTION_SKELETON = 0.030981  # calculus run5 build_skeleton / 40
PER_CONCEPT = 0.254655  # calculus run5 concept_page / 120
PER_IMAGE_CAPTION = 0.00026  # circuits caption / 242 captioned images (current v2)
ONCE_STRUCTURE = 1.026168  # calculus run0
ONCE_SPLIT = 0.044038  # calculus run5
ONCE_INDEX = 0.054848  # calculus run9 (latest index pass)

_TOK_SECTION_GENERATE = 371_743
_TOK_SECTION_SKELETON = 29_299
_TOK_CONCEPT = 82_808
_TOK_IMAGE_CAPTION = 171
_TOK_ONCE_STRUCTURE = 909_093
_TOK_ONCE_SPLIT = 42_234
_TOK_ONCE_INDEX = 806_592

# Scale ratios: turn raw input counts into section/concept/image counts. Strong anchor
# is PAGES_PER_SECTION_PDF (calculus 21.62 / circuits 21.60); the rest are weak.
PAGES_PER_SECTION_PDF = 21.6
SLIDES_PER_SECTION_PPTX = 3.2  # ai 29 slides / 9 sections (single sample)
CONCEPTS_PER_SECTION = 3.5  # calculus 3.0 ~ circuits 4.0, midpoint
CAPTION_KEEP_RATIO = 0.114  # circuits 242 captioned / 2126 embedded PDF images


@dataclass(frozen=True)
class Estimate:
    tokens: int
    cost_cny: float


@dataclass(frozen=True)
class Scale:
    sections: int
    concepts: int
    captioned_images: int


def estimate_cost(sections: int, concepts: int, captioned_images: int) -> Estimate:
    """Ideal-single-pass cost/token estimate for a book of the given scale."""
    cost = (
        sections * (PER_SECTION_GENERATE + PER_SECTION_SKELETON)
        + concepts * PER_CONCEPT
        + captioned_images * PER_IMAGE_CAPTION
        + ONCE_STRUCTURE
        + ONCE_SPLIT
        + ONCE_INDEX
    )
    tokens = (
        sections * (_TOK_SECTION_GENERATE + _TOK_SECTION_SKELETON)
        + concepts * _TOK_CONCEPT
        + captioned_images * _TOK_IMAGE_CAPTION
        + _TOK_ONCE_STRUCTURE
        + _TOK_ONCE_SPLIT
        + _TOK_ONCE_INDEX
    )
    return Estimate(tokens=int(tokens), cost_cny=round(cost, 6))


def derive_scale(scan: InputScan) -> Scale:
    """Turn raw ``input/`` counts into (sections, concepts, captioned_images).

    Sections come from content volume (PDF pages + PPTX slides), NOT file count —
    users split books at wildly different granularities (calculus 1 file/section,
    circuits 1 file/2 sections). Concepts and captioned images are weaker derivations.
    """
    sections = round(
        scan.pdf_pages / PAGES_PER_SECTION_PDF + scan.pptx_slides / SLIDES_PER_SECTION_PPTX
    )
    concepts = round(sections * CONCEPTS_PER_SECTION)
    captioned_images = round(scan.pdf_images * CAPTION_KEEP_RATIO)
    return Scale(sections=sections, concepts=concepts, captioned_images=captioned_images)
