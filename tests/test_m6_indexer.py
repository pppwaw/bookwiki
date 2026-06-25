from __future__ import annotations

import sqlite3
from pathlib import Path

from bookwiki.indexer.mdx_parser import parse_mdx_file
from bookwiki.indexer.rag_chunker import chunk_page
from bookwiki.indexer.sqlite_builder import build_sqlite_index


def test_parse_mdx_file_extracts_frontmatter_components_and_source_refs(tmp_path: Path) -> None:
    page = tmp_path / "content" / "docs" / "chapters" / "ch01.mdx"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n"
        "title: Search Basics\n"
        "type: chapter\n"
        "chapter_id: ch01\n"
        "concepts:\n"
        "  - Heuristic Search\n"
        "---\n"
        "# Search Basics\n\n"
        'A search frontier ranks nodes. <SourceRef id="textbook-p001" />\n\n'
        'Expression props also cite sources. <SourceRef id={"textbook-p002"} '
        'quote={"Expression prop quote"} />\n\n'
        "<QuizBlock>\n"
        '  <QuizItem id="q1" answer="choice-1" citations={[{"ref_id":"textbook-p001"}]}>\n'
        "    <QuizQuestion>Pick one</QuizQuestion>\n"
        "    <QuizChoices>\n"
        '    <QuizChoice id="choice-1">A</QuizChoice>\n'
        '    <QuizChoice id="choice-2">B</QuizChoice>\n'
        "    </QuizChoices>\n"
        "    <QuizCheck />\n"
        "    <QuizExplanation>Because</QuizExplanation>\n"
        "  </QuizItem>\n"
        "</QuizBlock>\n\n"
        '<AnkiDeck cardIds={["c1"]}>\n'
        '  <AnkiCard id="c1" citations={[{"ref_id":"lecture-slide02"}]}>\n'
        "    <AnkiFront>Front</AnkiFront>\n"
        "    <AnkiBack>Back</AnkiBack>\n"
        "  </AnkiCard>\n"
        "</AnkiDeck>\n",
        encoding="utf-8",
    )

    parsed = parse_mdx_file(page, root=tmp_path / "content" / "docs")

    assert parsed.id == "chapters/ch01"
    assert parsed.slug == "chapters/ch01"
    assert parsed.title == "Search Basics"
    assert parsed.type == "chapter"
    assert parsed.chapter_id == "ch01"
    assert parsed.frontmatter["concepts"] == ["Heuristic Search"]
    assert parsed.quiz_items[0]["question"] == "Pick one"
    assert parsed.card_items[0]["front"] == "Front"
    assert parsed.source_refs == ["textbook-p001", "textbook-p002", "lecture-slide02"]


def test_chunk_page_splits_on_headings_and_keeps_section_source_refs(tmp_path: Path) -> None:
    page = tmp_path / "content" / "docs" / "chapters" / "ch02.mdx"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n"
        "title: Heuristics\n"
        "type: chapter\n"
        "chapter_id: ch02\n"
        "---\n"
        "# Heuristics\n\n"
        'Intro text. <SourceRef id="textbook-p010" />\n\n'
        "## Admissibility\n\n"
        'Never overestimate the remaining cost. <SourceRef id={"textbook-p011"} />\n\n'
        "## Consistency\n\n"
        'Triangle inequality form. <SourceRef id="slides-slide03" />\n',
        encoding="utf-8",
    )
    parsed = parse_mdx_file(page, root=tmp_path / "content" / "docs")

    chunks = chunk_page(parsed, max_chars=120)

    assert [chunk.section_id for chunk in chunks] == ["heuristics", "admissibility", "consistency"]
    assert chunks[0].heading_path == "Heuristics"
    assert chunks[1].heading_path == "Heuristics > Admissibility"
    assert chunks[1].source_refs == ["textbook-p011"]
    assert chunks[2].source_refs == ["slides-slide03"]


def test_build_sqlite_index_writes_full_schema_fts_and_learning_items(tmp_path: Path) -> None:
    content_dir = tmp_path / "content" / "docs"
    chapter = content_dir / "chapters" / "ch01.mdx"
    concept = content_dir / "concepts" / "frontier.mdx"
    chapter.parent.mkdir(parents=True)
    concept.parent.mkdir(parents=True)
    chapter.write_text(
        "---\n"
        "title: Search Basics\n"
        "type: chapter\n"
        "chapter_id: ch01\n"
        "order_index: 1\n"
        "---\n"
        "# Search Basics\n\n"
        'A frontier stores generated nodes. <SourceRef id="textbook-p001" />\n\n'
        "## Practice\n\n"
        "<QuizBlock>\n"
        '  <QuizItem id="q1" answer="choice-1" citations={[{"ref_id":"textbook-p001"}]}>\n'
        "    <QuizQuestion>What stores nodes?</QuizQuestion>\n"
        "    <QuizChoices>\n"
        '    <QuizChoice id="choice-1">Frontier</QuizChoice>\n'
        '    <QuizChoice id="choice-2">Goal</QuizChoice>\n'
        "    </QuizChoices>\n"
        "    <QuizCheck />\n"
        "    <QuizExplanation>The frontier stores generated nodes.</QuizExplanation>\n"
        "  </QuizItem>\n"
        "</QuizBlock>\n\n"
        '<AnkiDeck cardIds={["c1"]}>\n'
        '  <AnkiCard id="c1" citations={[{"ref_id":"textbook-p001"}]}>\n'
        "    <AnkiFront>Frontier</AnkiFront>\n"
        "    <AnkiBack>Generated node container</AnkiBack>\n"
        "  </AnkiCard>\n"
        "</AnkiDeck>\n",
        encoding="utf-8",
    )
    concept.write_text(
        "---\n"
        "title: Frontier\n"
        "type: concept\n"
        "---\n"
        "# Frontier\n\n"
        'The frontier is the open list. <SourceRef id="lecture-slide02" />\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "site" / ".bookwiki" / "bookwiki.sqlite"

    result = build_sqlite_index(content_dir, db_path)

    assert result == db_path
    assert not db_path.with_suffix(".sqlite.tmp").exists()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type in ('table', 'view')"
            )
        }
        assert {
            "pages",
            "chunks",
            "fts_chunks",
            "quiz_items",
            "card_items",
            "source_refs",
            "documents",
        } <= tables
        chunk_columns = {row[1] for row in conn.execute("pragma table_info(chunks)").fetchall()}
        assert "rowid" in chunk_columns
        assert conn.execute("select count(*) from pages").fetchone()[0] == 2
        assert conn.execute("select count(*) from chunks").fetchone()[0] >= 2
        assert conn.execute("select question from quiz_items").fetchone()[0] == "What stores nodes?"
        assert conn.execute("select front from card_items").fetchone()[0] == "Frontier"
        assert conn.execute("select id from source_refs order by id").fetchall() == [
            ("lecture-slide02",),
            ("textbook-p001",),
        ]
        matches = conn.execute(
            "select chunks.chunk_id from chunks "
            "join fts_chunks on chunks.rowid = fts_chunks.rowid "
            "where fts_chunks match ?",
            ("frontier",),
        ).fetchall()
        assert matches


def test_build_sqlite_index_writes_worked_items_with_grading_json(tmp_path: Path) -> None:
    content_dir = tmp_path / "content" / "docs"
    chapter = content_dir / "chapters" / "ch03.mdx"
    chapter.parent.mkdir(parents=True)
    chapter.write_text(
        "---\n"
        "title: Worked Problems\n"
        "type: chapter\n"
        "chapter_id: ch03\n"
        "---\n"
        "# Worked Problems\n\n"
        '<WorkedProblem id="worked-001" chapterId="ch03" question={"证明 $a=b$。"} '
        'referenceAnswer={"由题设得 $a=b$。"} '
        'rubric={[{"point":"写出题设","weight":2}]} '
        'explanation={"考查证明过程。"} '
        'citations={[{"ref_id":"textbook-p001","quote":"a=b"}]}>'
        '</WorkedProblem>\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "site" / ".bookwiki" / "bookwiki.sqlite"

    parsed = parse_mdx_file(chapter, root=content_dir)
    build_sqlite_index(content_dir, db_path)

    assert parsed.quiz_items[0]["type"] == "worked"
    assert parsed.quiz_items[0]["grading_json"]["rubric"][0]["weight"] == 2
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select type, question, answer, grading_json, source_refs_json from quiz_items"
        ).fetchone()

    assert row[0] == "worked"
    assert row[1] == "证明 $a=b$。"
    assert row[2] == "由题设得 $a=b$。"
    assert '"reference_answer"' in row[3]
    assert '"textbook-p001"' in row[4]


def test_build_sqlite_index_dedupes_colliding_page_item_ids(tmp_path: Path) -> None:
    content_dir = tmp_path / "content" / "docs"
    chapter = content_dir / "chapters" / "ch01.mdx"
    chapter.parent.mkdir(parents=True)
    chapter.write_text(
        "---\n"
        "title: Duplicate Quiz IDs\n"
        "type: chapter\n"
        "chapter_id: ch01\n"
        "---\n"
        "# Duplicate Quiz IDs\n\n"
        "<QuizBlock>\n"
        '  <QuizItem answer="choice-1">\n'
        "    <QuizQuestion>Default id?</QuizQuestion>\n"
        "    <QuizChoices>\n"
        '    <QuizChoice id="choice-1">A</QuizChoice>\n'
        '    <QuizChoice id="choice-2">B</QuizChoice>\n'
        "    </QuizChoices>\n"
        "  </QuizItem>\n"
        '  <QuizItem id="quiz-001" answer="choice-2">\n'
        "    <QuizQuestion>Explicit colliding id?</QuizQuestion>\n"
        "    <QuizChoices>\n"
        '    <QuizChoice id="choice-1">A</QuizChoice>\n'
        '    <QuizChoice id="choice-2">B</QuizChoice>\n'
        "    </QuizChoices>\n"
        "  </QuizItem>\n"
        "</QuizBlock>\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "site" / ".bookwiki" / "bookwiki.sqlite"

    build_sqlite_index(content_dir, db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("select id, question from quiz_items order by question").fetchall()

    assert rows == [
        ("chapters/ch01:quiz-001", "Default id?"),
        ("chapters/ch01:quiz-001-002", "Explicit colliding id?"),
    ]
