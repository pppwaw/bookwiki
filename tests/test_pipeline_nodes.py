from __future__ import annotations

from bookwiki.integrator.markdown_renderers import normalize_public_asset_markdown_images
from bookwiki.pipeline.nodes import _drop_missing_local_markdown_links


def test_drop_missing_local_markdown_links_keeps_prose_and_existing_links(tmp_path) -> None:
    existing = tmp_path / "target.mdx"
    existing.write_text("# target\n", encoding="utf-8")
    body = (
        "See [valid](target), [missing](/Chapter-12-Three-phase-Circuits/section-003), "
        "[web](https://example.com), and `code [missing](/nope)`."
    )

    out = _drop_missing_local_markdown_links(body, tmp_path)

    assert "[valid](target)" in out
    assert "missing" in out
    assert "](/Chapter-12-Three-phase-Circuits/section-003)" not in out
    assert "[web](https://example.com)" in out
    assert "`code [missing](/nope)`" in out


def test_normalize_public_asset_markdown_images_makes_book_assets_root_relative() -> None:
    body = (
        "![figure](bookwiki-assets/source/figure.jpg) "
        "![local](images/local.jpg) "
        "![web](https://example.com/figure.jpg) "
        "`![code](bookwiki-assets/source/raw.jpg)`"
    )

    out = normalize_public_asset_markdown_images(body)

    assert "![figure](/bookwiki-assets/source/figure.jpg)" in out
    assert "![local](images/local.jpg)" in out
    assert "![web](https://example.com/figure.jpg)" in out
    assert "`![code](bookwiki-assets/source/raw.jpg)`" in out
