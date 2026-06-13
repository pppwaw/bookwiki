from __future__ import annotations

import json
import re
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
    assert "rehype-katex" in deps
    assert "katex" in deps

    source_config = (SITE / "source.config.ts").read_text(encoding="utf-8")
    next_config = (SITE / "next.config.mjs").read_text(encoding="utf-8")
    root_layout = (SITE / "app" / "layout.tsx").read_text(encoding="utf-8")
    source = (SITE / "lib" / "source.ts").read_text(encoding="utf-8")
    docs_layout = (SITE / "app" / "docs" / "layout.tsx").read_text(encoding="utf-8")
    docs_page = (SITE / "app" / "docs" / "[[...slug]]" / "page.tsx").read_text(
        encoding="utf-8"
    )
    shared = (SITE / "lib" / "shared.ts").read_text(encoding="utf-8")

    assert "defineDocs" in source_config
    assert 'dir: "content/docs"' in source_config
    assert "BOOKWIKI_CONTENT_DIR" not in source_config
    assert "providerImportSource" in source_config
    assert "remarkMath" in source_config
    assert "rehypeKatex" in source_config
    assert "rehypePlugins: (v) => [" in source_config
    assert '[rehypeKatex, { strict: false, output: "html" }],' in source_config
    assert "...v," in source_config
    assert "webpackBuildWorker: true" in next_config
    assert "katex/dist/katex.css" in root_layout
    assert "collections/server" in source
    assert "loader" in source
    assert "baseUrl: docsRoute" in source
    assert "getSourcePage" in source
    assert "TextDecoder('gbk')" in source
    assert "safeDecodeURIComponent(segment)" in source
    assert "encodeUtf8AsGbk(decodedSegment)" in source
    assert "docsRoute = '/docs'" in shared
    assert "DocsLayout" in docs_layout
    assert "source.getPageTree()" in docs_layout
    assert "AISearch" in root_layout
    assert "AISearchPanel" in root_layout
    assert "AISearchTrigger" in root_layout
    assert "generateStaticParams" in docs_page
    assert "getSourcePage(params.slug)" in docs_page
    assert "DocsPage" in docs_page
    assert "notFound()" in docs_page


def test_home_page_renders_generated_book_index() -> None:
    home_page = (SITE / "app" / "(home)" / "page.tsx").read_text(encoding="utf-8")

    assert "Hello World" not in home_page
    assert "getSourcePage(undefined)" in home_page
    assert "source.getPages()" in home_page
    assert "chapters[0]" in home_page


def test_site_template_build_script_uses_next_defaults() -> None:
    package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["build"] == "next build"


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
    preview_link = (SITE / "components" / "PreviewLink.tsx").read_text(encoding="utf-8")
    global_css = (SITE / "app" / "global.css").read_text(encoding="utf-8")

    for component in [
        "ConceptLink",
        "PreviewLink",
        "QuizBlock",
        "AnkiDeck",
        "SourceRef",
        "ChatBox",
    ]:
        assert component in mdx_components

    assert "SearchBox" not in mdx_components
    assert "MathCode" not in mdx_components
    assert "MathPre" not in mdx_components
    assert "./math" not in mdx_components
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
    assert "useChat" in chat_box
    assert "DefaultChatTransport" in chat_box
    assert "fetch(\"/api/chat\"" not in chat_box
    assert "Markdown" not in quiz_block
    assert "Markdown" not in anki_deck
    assert "children" in quiz_block
    assert "children" in anki_deck
    assert "remarkMath" in markdown
    assert "rehypeKatex" in markdown
    math_text = (SITE / "components" / "MathText.tsx").read_text(encoding="utf-8")
    assert "normalizeKatexInput" in math_text
    assert "KATEX_TEXT_MODE_DIGITS" in math_text
    assert "role=\"tooltip\"" in preview_link
    assert "preview-link-card" in preview_link
    assert ".preview-link-wrap:focus-within .preview-link-card" in global_css
    assert ".preview-link-wrap:hover .preview-link-card" in global_css
    assert "NEXT_PUBLIC_OPENAI_API_KEY" not in (sqlite + rag + chat_route + search_route)


def test_chapter_summary_uses_markdown_renderer() -> None:
    docs_page = (SITE / "app" / "docs" / "[[...slug]]" / "page.tsx").read_text(
        encoding="utf-8"
    )

    assert "import { Markdown } from '@/components/markdown';" in docs_page
    assert "<ChapterSummary><Markdown text={summary} /></ChapterSummary>" in docs_page
    assert "<ChapterSummary>{summary}</ChapterSummary>" not in docs_page


def test_quiz_block_item_effects_are_stable_against_context_state_updates() -> None:
    quiz_block = (SITE / "components" / "QuizBlock.tsx").read_text(encoding="utf-8")

    effect_deps = re.findall(
        r"useEffect\(\(\) => \s*\{(?P<body>.*?)\}\s*,\s*\[(?P<deps>[^\]]*)\]\s*\);",
        quiz_block,
        flags=re.DOTALL,
    )
    state_sync_deps = [
        deps
        for body, deps in effect_deps
        if "registerItem" in body or "setRecord" in body
    ]

    assert state_sync_deps
    assert all("deck" not in deps for deps in state_sync_deps)
    assert re.search(
        r"const existing = current\.get\(id\);.*?existing\?\.correct === value\.correct"
        r".*?return current;",
        quiz_block,
        flags=re.DOTALL,
    )


def test_official_ai_panel_is_backed_by_bookwiki_chat_api() -> None:
    package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    panel = (SITE / "components" / "ai" / "search.tsx").read_text(encoding="utf-8")
    chat_route = (SITE / "app" / "api" / "chat" / "route.ts").read_text(encoding="utf-8")
    rag = (SITE / "lib" / "rag.ts").read_text(encoding="utf-8")

    assert "BOOKWIKI_CHAT_API_KEY=" in env_example
    assert "BOOKWIKI_CHAT_BASE_URL=" in env_example
    assert "BOOKWIKI_CHAT_MODEL=" in env_example
    assert "OPENROUTER_API_KEY" not in env_example
    assert "ai" in package["dependencies"]
    assert "@ai-sdk/react" in package["dependencies"]
    assert "@openrouter/ai-sdk-provider" in package["dependencies"]
    assert "AISearchPanel" in panel
    assert "AISearchTrigger" in panel
    assert "useChat" in panel
    assert "DefaultChatTransport" in panel
    assert "prepareSendMessagesRequest" in panel
    assert "fetch(\"/api/chat\"" not in panel
    assert "usePathname" in panel
    assert "pagePath" in panel
    assert "@ai-sdk/react" in panel
    assert "createOpenRouter" in chat_route
    assert "streamText" in chat_route
    assert "toUIMessageStreamResponse" in chat_route
    assert "generateText" not in chat_route
    assert "providerOptions" in chat_route
    assert "reasoning: {" in chat_route
    assert "effort: 'low'" in chat_route
    assert "exclude: false" in chat_route
    assert "stepCountIs" in chat_route
    assert "tool(" in chat_route
    assert "OPENROUTER_API_KEY" not in chat_route
    assert "BOOKWIKI_CHAT_API_KEY" in chat_route
    assert "BOOKWIKI_CHAT_BASE_URL" in chat_route
    assert "BOOKWIKI_CHAT_MODEL" in chat_route
    assert "google/gemma-4-31b-it" in chat_route
    assert "search_book" in chat_route
    assert "get_current_article" in chat_route
    assert "chatFormatInstructions" in chat_route
    assert "[^Week-10-p008]" in chat_route
    assert "promptFromQuestion" in chat_route
    assert "<current_article>" in chat_route
    assert "answerText += part.text" in chat_route
    assert "citedSourcesFromText" in chat_route
    assert "searchChunks" in chat_route
    assert "answerWithChatModel" not in chat_route
    assert "currentArticleFromPath" in rag


def test_ai_chat_surfaces_reasoning_and_tool_parts() -> None:
    panel = (SITE / "components" / "ai" / "search.tsx").read_text(encoding="utf-8")
    chat_box = (SITE / "components" / "ChatBox.tsx").read_text(encoding="utf-8")

    for component in (panel, chat_box):
        assert "part.type === 'reasoning'" in component
        assert "part.type.startsWith('tool-')" in component
        assert "Reasoning" in component
        assert "Tool" in component
        assert "summarizeToolOutput" in component
        assert "Markdown" in component
        assert component.count("<Markdown text={part.text}") >= 2
        assert "<pre>{part.text}</pre>" not in component
        assert "whitespace-pre-wrap\">{part.text}</p>" not in component


def test_ai_markdown_renderer_supports_source_ref_citations() -> None:
    markdown = (SITE / "components" / "markdown.tsx").read_text(encoding="utf-8")

    assert "rehypeSourceRefs" in markdown
    assert "SourceRef" in markdown
    assert "sourceRefCitationPattern" in markdown
    assert "tagName: 'SourceRef'" in markdown


def test_ai_markdown_renderer_does_not_register_undefined_components() -> None:
    markdown = (SITE / "components" / "markdown.tsx").read_text(encoding="utf-8")

    assert "p: options?.inline ? InlineParagraph : undefined" not in markdown
    assert "img: undefined" not in markdown


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
    assert "<QuizChoices>" in joined_chapters
    assert "<QuizChoice" in joined_chapters
    assert "<QuizCheck />" in joined_chapters
    assert "<AnkiDeck" in joined_chapters
    assert "cardIds={" in joined_chapters
    assert "<AnkiCard" in joined_chapters
    assert "<AnkiFront>" in joined_chapters
    assert "<AnkiBack>" in joined_chapters
    assert "items={" not in joined_chapters
    assert "cards={" not in joined_chapters
    assert "<SourceRef" in joined_chapters
    assert "<ConceptLink" in joined_chapters
    assert "<SourceRef" in joined_concepts
