from __future__ import annotations

import pytest

from bookwiki.scheduler.dry_run import Estimate, Scale, derive_scale, estimate_cost
from bookwiki.scheduler.pdf_estimate import InputScan

# Mirror of the calibration constants, to assert linear scaling independent of value.
ONCE = 1.026168 + 0.044038 + 0.054848
PER_SECTION = 0.898002 + 0.030981
PER_CONCEPT = 0.254655
PER_IMAGE = 0.00026


def test_zero_scale_is_only_one_shot_cost() -> None:
    assert estimate_cost(0, 0, 0).cost_cny == pytest.approx(ONCE, abs=1e-4)


def test_full_calculus_scale_reproduces_measured_run() -> None:
    # 40 sections, 120 concepts, no captioned images ≈ the measured clean pass.
    assert estimate_cost(40, 120, 0).cost_cny == pytest.approx(68.84, abs=0.5)


def test_sections_scale_generate_and_skeleton() -> None:
    base = estimate_cost(0, 0, 0).cost_cny
    assert estimate_cost(10, 0, 0).cost_cny - base == pytest.approx(10 * PER_SECTION, abs=1e-4)


def test_concepts_scale_linearly() -> None:
    base = estimate_cost(0, 0, 0).cost_cny
    assert estimate_cost(0, 10, 0).cost_cny - base == pytest.approx(10 * PER_CONCEPT, abs=1e-4)


def test_captioned_images_scale_linearly() -> None:
    base = estimate_cost(0, 0, 0).cost_cny
    assert estimate_cost(0, 0, 1000).cost_cny - base == pytest.approx(1000 * PER_IMAGE, abs=1e-4)


def test_returns_estimate_with_positive_tokens() -> None:
    result = estimate_cost(40, 120, 0)
    assert isinstance(result, Estimate)
    assert result.tokens > 0


def test_derive_scale_pdf_pages_to_sections() -> None:
    # calculus: 865 PDF pages → 40 sections (≈ 21.6 pages/section)
    assert derive_scale(InputScan(pdf_pages=865)).sections == 40


def test_derive_scale_pptx_slides_to_sections() -> None:
    # ai: 29 slides → 9 sections (≈ 3.2 slides/section)
    assert derive_scale(InputScan(pptx_slides=29)).sections == 9


def test_derive_scale_concepts_from_sections() -> None:
    scale = derive_scale(InputScan(pdf_pages=865))
    assert isinstance(scale, Scale)
    assert scale.concepts == round(scale.sections * 3.5)


def test_derive_scale_captioned_images_from_pdf_images() -> None:
    # circuits: 2126 embedded PDF images → ~242 captioned (keep ratio ≈ 0.114)
    assert derive_scale(InputScan(pdf_images=2126)).captioned_images == round(2126 * 0.114)
