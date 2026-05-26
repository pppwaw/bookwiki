from __future__ import annotations

import json
import sys

from bookwiki.scheduler.config import default_config, load_config, save_config
from scripts import site

DEFAULT_GENERATION_EXPECTED = {
    "quizPerChapter": 5,
    "cardsPerChapter": 8,
    "sourceLayoutRepair": {
        "mode": "auto",
        "minConfidence": 0.85,
        "maxCandidatesPerSource": 20,
    },
}


def test_default_config_writes_language_and_generation_defaults(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    cfg = default_config(book_dir)

    assert cfg.language == "zh-CN"
    assert cfg.generation == DEFAULT_GENERATION_EXPECTED

    config_path = save_config(cfg)
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["language"] == "zh-CN"
    assert payload["generation"] == DEFAULT_GENERATION_EXPECTED


def test_load_config_defaults_language_and_generation_for_existing_config(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}, ensure_ascii=False),
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.language == "zh-CN"
    assert cfg.generation == DEFAULT_GENERATION_EXPECTED


def test_load_config_merges_source_layout_repair_defaults(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps(
            {
                "book_id": "mini",
                "title": "Mini",
                "generation": {"sourceLayoutRepair": {"mode": "off"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.generation["sourceLayoutRepair"] == {
        "mode": "off",
        "minConfidence": 0.85,
        "maxCandidatesPerSource": 20,
    }


def test_site_main_sets_site_language_from_book_config(
    tmp_path, monkeypatch
) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text(
        "---\ntitle: Mini\n---\n\n# Mini Book\n",
        encoding="utf-8",
    )
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    sqlite_dir = book_dir / "site" / ".bookwiki"
    sqlite_dir.mkdir(parents=True)
    sqlite_path = sqlite_dir / "bookwiki.sqlite"
    sqlite_path.write_bytes(b"sqlite fixture")
    next_cache = book_dir / "site" / ".next" / "dev" / "logs"
    next_cache.mkdir(parents=True)
    (next_cache / "next-development.log").write_text("cache", encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini", "language": "en-US"}),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_run(cmd, *, cwd, env, check):  # noqa: ANN001
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "check": check})

    monkeypatch.setattr(sys, "argv", ["site.py", str(book_dir)])
    monkeypatch.setattr(site.subprocess, "run", fake_run)

    site.main()

    assert calls
    assert calls[0]["cmd"] == ["pnpm", "install"]
    assert calls[-1]["cmd"] == ["pnpm", "dev"]
    assert calls[-1]["cwd"] == book_dir / "site"
    assert calls[-1]["env"]["BOOKWIKI_SITE_LANGUAGE"] == "en-US"
    assert "BOOKWIKI_CONTENT_DIR" not in calls[-1]["env"]
    assert "BOOKWIKI_SQLITE_PATH" not in calls[-1]["env"]
    assert "BOOKWIKI_BOOK_DIR" not in calls[-1]["env"]
    assert (book_dir / "site" / "package.json").exists()
    assert (book_dir / "site" / "content" / "docs" / "index.mdx").read_text(
        encoding="utf-8"
    ).endswith("# Mini Book\n")
    assert sqlite_path.read_bytes() == b"sqlite fixture"
    assert (next_cache / "next-development.log").read_text(encoding="utf-8") == "cache"


def test_site_main_fails_loudly_when_content_docs_is_missing(
    tmp_path, monkeypatch
) -> None:
    book_dir = tmp_path / "mini"
    book_dir.mkdir()
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}),
        encoding="utf-8",
    )
    alternate = site.ROOT / "books" / "mini"
    monkeypatch.setattr(sys, "argv", ["site.py", str(book_dir)])

    try:
        site.main()
    except FileNotFoundError as exc:
        message = str(exc)
    else:  # pragma: no cover
        raise AssertionError("site.main should fail when content/docs is missing")

    assert "content" in message
    if (alternate / "content" / "docs").exists():
        assert "Did you mean" in message
