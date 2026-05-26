from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site-template"


def test_site_template_uses_fumadocs_official_mdx_collection_shape() -> None:
    package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
    deps = package["dependencies"]

    assert "fumadocs-core" in deps
    assert "fumadocs-mdx" in deps
    assert "fumadocs-ui" in deps
    assert "better-sqlite3" in deps
    assert "remark-math" in deps
    assert "katex" in deps
    assert "rehype-katex" not in deps

    source_config = (SITE / "source.config.ts").read_text(encoding="utf-8")
    root_layout = (SITE / "app" / "layout.tsx").read_text(encoding="utf-8")
    source = (SITE / "lib" / "source.ts").read_text(encoding="utf-8")
    docs_layout = (SITE / "app" / "docs" / "layout.tsx").read_text(encoding="utf-8")
    docs_page = (SITE / "app" / "docs" / "[[...slug]]" / "page.tsx").read_text(
        encoding="utf-8"
    )
    shared = (SITE / "lib" / "shared.ts").read_text(encoding="utf-8")

    assert "defineDocs" in source_config
    assert "dir: 'content/docs'" in source_config
    assert "BOOKWIKI_CONTENT_DIR" not in source_config
    assert "providerImportSource" in source_config
    assert "remarkMath" in source_config
    assert "rehypeKatex" not in source_config
    assert "rehypePlugins" not in source_config
    assert "katex/dist/katex.css" in root_layout
    assert "collections/server" in source
    assert "loader" in source
    assert "baseUrl: docsRoute" in source
    assert "docsRoute = '/docs'" in shared
    assert "DocsLayout" in docs_layout
    assert "source.getPageTree()" in docs_layout
    assert "AISearch" in docs_layout
    assert "AISearchPanel" in docs_layout
    assert "AISearchTrigger" in docs_layout
    assert "generateStaticParams" in docs_page
    assert "source.getPage" in docs_page
    assert "DocsPage" in docs_page
    assert "notFound()" in docs_page


def test_site_template_wires_bookwiki_components_and_server_only_data_paths() -> None:
    mdx_components = (SITE / "components" / "mdx.tsx").read_text(encoding="utf-8")
    sqlite = (SITE / "lib" / "sqlite.ts").read_text(encoding="utf-8")
    rag = (SITE / "lib" / "rag.ts").read_text(encoding="utf-8")
    chat_route = (SITE / "app" / "api" / "chat" / "route.ts").read_text(encoding="utf-8")
    search_route = (SITE / "app" / "api" / "search" / "route.ts").read_text(encoding="utf-8")
    chat_box = (SITE / "components" / "ChatBox.tsx").read_text(encoding="utf-8")
    quiz_block = (SITE / "components" / "QuizBlock.tsx").read_text(encoding="utf-8")
    anki_deck = (SITE / "components" / "AnkiDeck.tsx").read_text(encoding="utf-8")
    markdown = (SITE / "components" / "markdown.tsx").read_text(encoding="utf-8")

    for component in [
        "ConceptLink",
        "QuizBlock",
        "AnkiDeck",
        "SourceRef",
        "ChatBox",
    ]:
        assert component in mdx_components

    assert "SearchBox" not in mdx_components
    assert "MathCode" in mdx_components
    assert "MathPre" in mdx_components
    assert not (SITE / "components" / "SearchBox.tsx").exists()
    assert not (SITE / "app" / "api" / "bookwiki" / "search" / "route.ts").exists()
    assert "readonly: true" in sqlite
    assert "better-sqlite3" in sqlite
    assert ".bookwiki" in sqlite
    assert "BOOKWIKI_SQLITE_PATH" not in sqlite
    assert "BOOKWIKI_BOOK_DIR" not in sqlite
    assert "fts_chunks MATCH" in rag
    assert "BOOKWIKI_CHAT_MODEL" in chat_route
    assert "createFromSource" in search_route
    assert "searchChunks" not in search_route
    assert "searchChunks" in chat_route
    assert "fetch(\"/api/chat\"" in chat_box
    assert "Markdown" not in quiz_block
    assert "Markdown" not in anki_deck
    assert "children" in quiz_block
    assert "children" in anki_deck
    assert "splitMathSegments" in markdown
    assert "remarkMath" not in markdown
    assert "rehypeKatex" not in markdown
    assert "NEXT_PUBLIC_OPENAI_API_KEY" not in (sqlite + rag + chat_route + search_route)


def test_official_ai_panel_is_backed_by_bookwiki_chat_api() -> None:
    panel = (SITE / "components" / "ai" / "search.tsx").read_text(encoding="utf-8")
    chat_route = (SITE / "app" / "api" / "chat" / "route.ts").read_text(encoding="utf-8")

    assert "AISearchPanel" in panel
    assert "AISearchTrigger" in panel
    assert "fetch(\"/api/chat\"" in panel
    assert "@ai-sdk/react" not in panel
    assert "searchChunks" in chat_route
    assert "answerWithChatModel" in chat_route


def test_site_template_contains_sample_mdx_content_for_m6a() -> None:
    chapters = sorted((SITE / "content" / "docs" / "chapters").glob("*.mdx"))
    concepts = sorted((SITE / "content" / "docs" / "concepts").glob("*.mdx"))
    meta = json.loads((SITE / "content" / "docs" / "meta.json").read_text(encoding="utf-8"))

    assert len(chapters) >= 2
    assert len(concepts) >= 2
    assert "pages" in meta

    joined_chapters = "\n".join(path.read_text(encoding="utf-8") for path in chapters)
    joined_concepts = "\n".join(path.read_text(encoding="utf-8") for path in concepts)

    assert "<QuizBlock" in joined_chapters
    assert "<QuizItem" in joined_chapters
    assert "<QuizQuestion>" in joined_chapters
    assert "<QuizChoice" in joined_chapters
    assert "<AnkiDeck" in joined_chapters
    assert "<AnkiCard" in joined_chapters
    assert "<AnkiFront>" in joined_chapters
    assert "<AnkiBack>" in joined_chapters
    assert "items={" not in joined_chapters
    assert "cards={" not in joined_chapters
    assert "<SourceRef" in joined_chapters
    assert "<ConceptLink" in joined_chapters
    assert "<SourceRef" in joined_concepts
