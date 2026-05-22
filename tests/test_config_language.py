from __future__ import annotations

import json
import sys

from bookwiki.scheduler.config import default_config, load_config, save_config
from scripts import site


def test_default_config_writes_language_and_generation_defaults(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    cfg = default_config(book_dir)

    assert cfg.language == "zh-CN"
    assert cfg.generation == {"quizPerChapter": 5, "cardsPerChapter": 8}

    config_path = save_config(cfg)
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["language"] == "zh-CN"
    assert payload["generation"] == {"quizPerChapter": 5, "cardsPerChapter": 8}


def test_load_config_defaults_language_and_generation_for_existing_config(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}, ensure_ascii=False),
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.language == "zh-CN"
    assert cfg.generation == {"quizPerChapter": 5, "cardsPerChapter": 8}


def test_site_main_sets_site_language_from_book_config(
    tmp_path, monkeypatch
) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
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
    assert calls[0]["env"]["BOOKWIKI_SITE_LANGUAGE"] == "en-US"
