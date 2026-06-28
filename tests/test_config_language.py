from __future__ import annotations

import json
import sys

from bookwiki.scheduler.config import default_config, load_config, save_config
from scripts import site
from scripts.init_book import init_book

DEFAULT_GENERATION_EXPECTED = {
    "quizPerChapter": 5,
    "cardsPerChapter": 8,
    "maxChapterConcurrency": 4,
    "maxSectionConcurrency": 3,
    "maxRepairRounds": 3,
    "qualityCheck": False,
    "maxQualityRounds": 2,
    "allowMissingMdxValidator": False,
    "siteTypeCheck": "auto",
    "sourceLayoutRepair": {
        "mode": "auto",
        "minConfidence": 0.85,
        "maxCandidatesPerSource": 20,
    },
    "visionCaption": {
        "mode": "auto",
        "maxImagesPerSource": 200,
        "maxConcurrent": 10,
    },
}


def test_default_config_writes_language_and_generation_defaults(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    cfg = default_config(book_dir)

    assert cfg.language == "zh-CN"
    assert cfg.generation == DEFAULT_GENERATION_EXPECTED
    assert cfg.notes_path == "book.notes.md"
    assert "section" in cfg.models
    assert "quiz" not in cfg.models
    assert cfg.models["vision"] == "openrouter-qwen3.6-35b-a3b"
    assert cfg.budget == {"maxCostCny": 70.0}

    config_path = save_config(cfg)
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["language"] == "zh-CN"
    assert payload["generation"] == DEFAULT_GENERATION_EXPECTED
    assert payload["notesPath"] == "book.notes.md"
    assert "section" in payload["models"]
    assert "quiz" not in payload["models"]
    assert payload["models"]["vision"] == "openrouter-qwen3.6-35b-a3b"
    assert payload["budget"] == {"maxCostCny": 70.0}


def test_load_config_keeps_budget_current_schema_only(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps(
            {
                "book_id": "mini",
                "title": "Mini",
                "budget": {"maxCostUsd": 12, "maxCostCny": 34},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.budget == {"maxCostCny": 34}


def test_load_config_defaults_to_budget_limit(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}, ensure_ascii=False),
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.budget == {"maxCostCny": 70.0}


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
    assert cfg.notes_path == "book.notes.md"
    assert cfg.book_notes == ""


def test_load_config_reads_user_book_notes(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps(
            {"book_id": "mini", "title": "Mini", "notesPath": "course-notes.md"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (book_dir / "course-notes.md").write_text(
        "English teaching: include English terms for every concept.",
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.notes_path == "course-notes.md"
    assert cfg.book_notes == "English teaching: include English terms for every concept."


def test_init_book_creates_editable_book_notes_template(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"

    init_book(book_dir)

    notes_path = book_dir / "book.notes.md"
    assert notes_path.exists()
    notes = notes_path.read_text(encoding="utf-8")
    # The template is intentionally minimal (the teaching/source guidance was removed); it only
    # seeds an editable "# Book Notes" heading for the user to fill in.
    assert notes.strip() == "# Book Notes"


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
    assert cfg.generation["visionCaption"] == {
        "mode": "auto",
        "maxImagesPerSource": 200,
        "maxConcurrent": 10,
    }


def test_load_config_merges_vision_caption_defaults(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    (book_dir / "book.config.json").write_text(
        json.dumps(
            {
                "book_id": "mini",
                "title": "Mini",
                "generation": {"visionCaption": {"mode": "off"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg = load_config(book_dir)

    assert cfg.generation["visionCaption"] == {
        "mode": "off",
        "maxImagesPerSource": 200,
        "maxConcurrent": 10,
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
    assert calls[1]["cmd"] == ["pnpm", "build"]
    assert calls[-1]["cmd"] == ["pnpm", "start"]
    assert calls[-1]["cwd"] == book_dir / "site"
    assert calls[-1]["env"]["BOOKWIKI_SITE_LANGUAGE"] == "en-US"
    assert calls[-1]["env"]["NODE_OPTIONS"] == "--max-old-space-size=4096"
    assert "BOOKWIKI_CONTENT_DIR" not in calls[-1]["env"]
    assert "BOOKWIKI_SQLITE_PATH" not in calls[-1]["env"]
    assert "BOOKWIKI_BOOK_DIR" not in calls[-1]["env"]
    assert (book_dir / "site" / "package.json").exists()
    assert (book_dir / "site" / "content" / "docs" / "index.mdx").read_text(
        encoding="utf-8"
    ).endswith("# Mini Book\n")
    assets_dir = book_dir / "work" / "assets" / "mini"
    assets_dir.mkdir(parents=True)
    (assets_dir / "figure.png").write_bytes(b"image")
    site.materialize_site(load_config(book_dir))
    assert (
        book_dir / "site" / "public" / "bookwiki-assets" / "mini" / "figure.png"
    ).read_bytes() == b"image"
    assert sqlite_path.read_bytes() == b"sqlite fixture"
    # .next build cache survives materialize (persistent workspace → incremental builds).
    assert (next_cache / "next-development.log").read_text(encoding="utf-8") == "cache"


def test_site_main_syncs_only_chat_env_to_site_env_file(tmp_path, monkeypatch) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini", "language": "en-US"}),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "BOOKWIKI_CHAT_API_KEY=chat-from-dotenv",
                "BOOKWIKI_CHAT_BASE_URL=https://openrouter.ai/api/v1",
                "BOOKWIKI_CHAT_MODEL=google/gemma-4-31b-it",
                "OPENROUTER_API_KEY=do-not-copy",
                "DEEPSEEK_API_KEY=do-not-copy",
                "",
            ]
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_run(cmd, *, cwd, env, check):  # noqa: ANN001
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "check": check})

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOKWIKI_CHAT_API_KEY", raising=False)
    monkeypatch.delenv("BOOKWIKI_CHAT_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BOOKWIKI_CHAT_MODEL", raising=False)
    monkeypatch.setattr(sys, "argv", ["site.py", str(book_dir)])
    monkeypatch.setattr(site.subprocess, "run", fake_run)

    site.main()

    assert calls
    site_env = (book_dir / "site" / ".env.local").read_text(encoding="utf-8")
    assert "BOOKWIKI_CHAT_API_KEY=chat-from-dotenv" in site_env
    assert "BOOKWIKI_CHAT_BASE_URL=https://openrouter.ai/api/v1" in site_env
    assert "BOOKWIKI_CHAT_MODEL=google/gemma-4-31b-it" in site_env
    assert "OPENROUTER_API_KEY" not in site_env
    assert "DEEPSEEK_API_KEY" not in site_env


def test_site_main_preserves_existing_node_options(tmp_path, monkeypatch) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini", "language": "en-US"}),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_run(cmd, *, cwd, env, check):  # noqa: ANN001
        calls.append({"cmd": cmd, "cwd": cwd, "env": env, "check": check})

    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=8192")
    monkeypatch.setattr(sys, "argv", ["site.py", str(book_dir)])
    monkeypatch.setattr(site.subprocess, "run", fake_run)

    site.main()

    assert calls[-1]["env"]["NODE_OPTIONS"] == "--max-old-space-size=8192"


def test_materialize_site_preserves_local_env_file(tmp_path) -> None:
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}),
        encoding="utf-8",
    )
    env_path = book_dir / "site" / ".env.local"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("BOOKWIKI_CHAT_API_KEY=book-local\n", encoding="utf-8")

    site.materialize_site(load_config(book_dir))

    assert env_path.read_text(encoding="utf-8") == "BOOKWIKI_CHAT_API_KEY=book-local\n"


def test_sync_public_book_id_writes_and_is_idempotent(tmp_path) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    env_path = site_dir / ".env.local"
    env_path.write_text("BOOKWIKI_CHAT_API_KEY=book-local\n", encoding="utf-8")

    site.sync_public_book_id(site_dir, "calculus")
    first = env_path.read_text(encoding="utf-8")
    assert "BOOKWIKI_CHAT_API_KEY=book-local" in first
    assert "NEXT_PUBLIC_BOOK_ID=calculus" in first

    # Re-running with a new id replaces, never duplicates, the line.
    site.sync_public_book_id(site_dir, "circuits")
    second = env_path.read_text(encoding="utf-8")
    assert second.count("NEXT_PUBLIC_BOOK_ID=") == 1
    assert "NEXT_PUBLIC_BOOK_ID=circuits" in second
    assert "BOOKWIKI_CHAT_API_KEY=book-local" in second


def test_materialize_site_preserves_build_caches(tmp_path) -> None:
    # site is a persistent workspace now: .next/.source build caches must survive re-materialize so
    # the next ``pnpm build`` is incremental. Next.js/fumadocs invalidate their own caches by file
    # hash, so keeping them is safe (and the industry norm — CI caches .next).
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}),
        encoding="utf-8",
    )
    site_dir = book_dir / "site"
    source_cache = site_dir / ".source"
    next_cache = site_dir / ".next"
    source_cache.mkdir(parents=True)
    next_cache.mkdir()
    (source_cache / "keep.txt").write_text("incremental mdx", encoding="utf-8")
    (next_cache / "keep.txt").write_text("incremental build", encoding="utf-8")

    site.materialize_site(load_config(book_dir))

    assert (source_cache / "keep.txt").read_text(encoding="utf-8") == "incremental mdx"
    assert (next_cache / "keep.txt").read_text(encoding="utf-8") == "incremental build"


def test_materialize_site_preserves_node_modules(tmp_path) -> None:
    # Dependencies are independent of book content; deleting node_modules every materialize only
    # forces a costly reinstall. Persistent workspace keeps them.
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}),
        encoding="utf-8",
    )
    node_modules = book_dir / "site" / "node_modules"
    node_modules.mkdir(parents=True)
    (node_modules / "marker.txt").write_text("installed deps", encoding="utf-8")

    site.materialize_site(load_config(book_dir))

    assert (node_modules / "marker.txt").read_text(encoding="utf-8") == "installed deps"


def test_materialize_site_preserves_symlinked_node_modules(tmp_path) -> None:
    # A symlinked node_modules (shared store) is likewise preserved, not severed.
    book_dir = tmp_path / "books" / "mini"
    book_dir.mkdir(parents=True)
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    (book_dir / "book.config.json").write_text(
        json.dumps({"book_id": "mini", "title": "Mini"}),
        encoding="utf-8",
    )
    target = tmp_path / "shared-node-modules"
    target.mkdir()
    (target / "marker.txt").write_text("shared deps", encoding="utf-8")
    site_dir = book_dir / "site"
    site_dir.mkdir()
    node_modules = site_dir / "node_modules"
    node_modules.symlink_to(target, target_is_directory=True)

    site.materialize_site(load_config(book_dir))

    assert node_modules.is_symlink()
    assert (node_modules / "marker.txt").exists()


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
