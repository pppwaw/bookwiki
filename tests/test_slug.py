from __future__ import annotations

import unicodedata

from bookwiki.convert.common import slugify_path_segment


def test_slugify_preserves_cjk() -> None:
    assert slugify_path_segment("知识图谱总览") == "知识图谱总览"
    assert slugify_path_segment("欧姆定律与基尔霍夫定律") == "欧姆定律与基尔霍夫定律"


def test_slugify_replaces_separators_and_keeps_internal_dot() -> None:
    assert slugify_path_segment("Chapter 6 Point Estimation") == "Chapter-6-Point-Estimation"
    # An internal dot (e.g. a section number) is kept; spaces collapse to a single hyphen.
    assert slugify_path_segment("9.2 Infinite Series") == "9.2-Infinite-Series"


def test_slugify_bans_ascii_and_fullwidth_colon() -> None:
    # Colons must never survive: owner_task_id "<id>:<kind>" parsing relies on it.
    assert ":" not in slugify_path_segment("Chapter 6: Point Estimation")
    assert "：" not in slugify_path_segment("第6章：点估计")


def test_slugify_rejects_path_traversal_and_dot_segments() -> None:
    assert "/" not in slugify_path_segment("../../etc/passwd")
    # Pure dot segments are unsafe path names and fall back to a hashed slug.
    for danger in (".", "..", "..."):
        result = slugify_path_segment(danger, fallback_prefix="chapter")
        assert result.startswith("chapter-")
        assert result not in {".", ".."}


def test_slugify_falls_back_for_empty_and_punctuation_only() -> None:
    assert slugify_path_segment("   ", fallback_prefix="chapter").startswith("chapter-")
    assert slugify_path_segment("!!!", fallback_prefix="chapter").startswith("chapter-")


def test_slugify_guards_windows_reserved_names() -> None:
    for reserved in ("con", "CON", "nul", "com1", "LPT9"):
        assert slugify_path_segment(reserved, fallback_prefix="chapter").startswith("chapter-")


def test_slugify_is_deterministic_and_nfc_normalised() -> None:
    # Same input → same slug (stable across runs / cache keys).
    assert slugify_path_segment("点估计") == slugify_path_segment("点估计")
    # NFC vs NFD forms of the same text map to one slug.
    nfd = unicodedata.normalize("NFD", "é语")
    nfc = unicodedata.normalize("NFC", "é语")
    assert slugify_path_segment(nfd) == slugify_path_segment(nfc)


def test_slugify_caps_length_with_stable_hash_tail() -> None:
    long_name = "A" * 200
    result = slugify_path_segment(long_name)
    assert len(result) <= 80
    # Deterministic: the truncation + hash tail is stable.
    assert result == slugify_path_segment(long_name)
