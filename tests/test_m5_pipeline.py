from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bookwiki.agents.concept_extract import ConceptExtractAgent
from bookwiki.agents.concept_reconcile import ConceptReconcileAgent
from bookwiki.pipeline.nodes import (
    check_node,
    concept_pages_node,
    integrate_node,
    reconcile_node,
    repair_node,
)
from bookwiki.scheduler.config import BookConfig
from bookwiki.scheduler.llm import TestLLMRuntime
from scripts import site
from tests.fakes import RecordingRuntime


def _write_minimal_site_template(path) -> None:  # noqa: ANN001
    path.mkdir(parents=True)
    (path / "package.json").write_text(
        json.dumps({"scripts": {"types:check": "tsc --noEmit"}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_concept_extract_defaults_to_chapter_result_concepts_without_llm(tmp_path) -> None:
    cfg = BookConfig(
        book_dir=tmp_path / "book",
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    agent = ConceptExtractAgent()

    result = await agent.run(
        {
            "chapter_id": "chapter-1",
            "title": "Search",
            "concepts": ["递归", "递推"],
        },
        model=cfg.model_for("concept"),
        runtime=cfg.llm_runtime,
    )

    assert [item.name for item in result.concepts] == ["递归", "递推"]
    assert result.concepts[0].source_chapter_id == "chapter-1"
    assert result.concepts[0].owner_task_id == "chapter-1:concept_extract"


@pytest.mark.asyncio
async def test_concept_reconcile_merges_aliases_rule_first_without_llm() -> None:
    agent = ConceptReconcileAgent()

    result = await agent.run(
        [
            {
                "name": "递归",
                "aliases": ["递推"],
                "source_chapter_id": "chapter-1",
                "owner_task_id": "chapter-1:concept_extract",
            },
            {
                "name": "递推",
                "aliases": [],
                "source_chapter_id": "chapter-2",
                "owner_task_id": "chapter-2:concept_extract",
            },
        ],
        model="deepseek-v4-pro",
        runtime=TestLLMRuntime(),
    )

    assert [item.canonical for item in result.concepts] == ["递归"]
    assert result.concepts[0].source_chapter_ids == ["chapter-1", "chapter-2"]
    assert result.alias_map["递推"] == "递归"


@pytest.mark.asyncio
async def test_check_routes_bad_quiz_answer_and_repair_drops_invalid_item(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    result_dir.mkdir(parents=True)
    (result_dir / "chapter-1.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "title": "Search",
                    "body_md": (
                        "Search uses [[递推]].\n\n"
                        '<QuizBlock>\n<QuizItemSlot id="chapter-1:s0:slot-000" '
                        'topic="t" sourceRefs={["source-p001"]} />\n'
                        '<QuizItemSlot id="chapter-1:s0:slot-001" '
                        'topic="t" sourceRefs={["source-p001"]} />\n</QuizBlock>'
                    ),
                    "concepts": ["递归"],
                    "citations": [{"ref_id": "source-p001", "quote": "Search source"}],
                    "owner_task_id": "chapter-1:chapter",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.summary.json").write_text(
        json.dumps(
            {"result": {"summary_md": "Search summary.", "owner_task_id": "chapter-1:summary"}}
        ),
        encoding="utf-8",
    )
    quiz_path = result_dir / "chapter-1.quiz.json"
    quiz_path.write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "items": [
                        {
                            "question": "Valid question?",
                            "choices": ["A", "B"],
                            "answer": "A",
                            "explanation": "Valid.",
                            "citations": [],
                            "slot_id": "chapter-1:s0:slot-000",
                        },
                        {
                            "question": "What is search?",
                            "choices": ["A", "B"],
                            "answer": "C",
                            "explanation": "Because.",
                            "citations": [{"ref_id": "source-p001", "quote": "Search source"}],
                            "slot_id": "chapter-1:s0:slot-001",
                        },
                    ],
                    "owner_task_id": "chapter-1:quiz",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.card.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "items": [{"front": "Search", "back": "Find a path.", "citations": []}],
                    "owner_task_id": "chapter-1:card",
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        },
        "alias_map": {"递推": "递归"},
        "concept_pages": {},
    }

    integrate_node(state, cfg)
    checked = await check_node(state, cfg)

    assert checked["repair_targets"] == ["chapter-1:quiz"]
    report = json.loads((book_dir / "work" / "logs" / "check-report.json").read_text())
    assert report["issues"][0]["code"] == "QUIZ_ANSWER_NOT_IN_CHOICES"
    assert report["issues"][0]["owner_task_id"] == "chapter-1:quiz"

    repaired = await repair_node({**state, **checked}, cfg)
    assert repaired["repair_targets"] == []
    repaired_payload = json.loads(quiz_path.read_text(encoding="utf-8"))
    # The bad item is DROPPED (not silently rewritten to a wrong answer); the valid
    # item is kept so the chapter still has a quiz.
    items = repaired_payload["result"]["items"]
    assert len(items) == 1
    assert items[0]["question"] == "Valid question?"
    actions = json.loads(
        (book_dir / "work" / "logs" / "repair-actions.json").read_text(encoding="utf-8")
    )
    assert actions["actions"][0]["owner_task_id"] == "chapter-1:quiz"
    assert actions["actions"][0]["dropped_quiz_items"] == ["What is search?"]

    integrate_node(state, cfg)
    passed = await check_node(state, cfg)
    assert passed["repair_targets"] == []


@pytest.mark.asyncio
async def test_check_accepts_hyphenated_source_refs_from_sources_md(tmp_path) -> None:
    book_dir = tmp_path / "book"
    sources_dir = book_dir / "work" / "sources_md"
    result_dir = book_dir / "work" / "agent_results"
    sources_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    (sources_dir / "Week-10.md").write_text(
        "# Source\n\n<!-- source_ref: Week-10-p001 -->\n\nSource text.",
        encoding="utf-8",
    )
    (result_dir / "chapter-6.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "title": "Point Estimation",
                    "body_md": (
                        "Body.\n\n"
                        '<QuizBlock>\n<QuizItemSlot id="chapter-6:s0:slot-000" '
                        'topic="t" sourceRefs={["Week-10-p001"]} />\n</QuizBlock>'
                    ),
                    "concepts": [],
                    "citations": [{"ref_id": "Week-10-p001", "quote": "Source text"}],
                    "owner_task_id": "chapter-6:chapter",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.summary.json").write_text(
        json.dumps(
            {
                "result": {
                    "summary_md": "Summary.",
                    "citations": [{"ref_id": "Week-10-p001", "quote": "Source text"}],
                    "owner_task_id": "chapter-6:summary",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [
                        {
                            "question": "Q?",
                            "choices": ["A", "B"],
                            "answer": "A",
                            "explanation": "E.",
                            "citations": [{"ref_id": "Week-10-p001", "quote": "Source text"}],
                            "slot_id": "chapter-6:s0:slot-000",
                        }
                    ],
                    "owner_task_id": "chapter-6:quiz",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.card.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [
                        {
                            "front": "Front",
                            "back": "Back",
                            "citations": [{"ref_id": "Week-10-p001", "quote": "Source text"}],
                        }
                    ],
                    "owner_task_id": "chapter-6:card",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "sources_md": ["work/sources_md/Week-10.md"],
        "agent_results": {
            "chapter-6": {
                "chapter": "work/agent_results/chapter-6.chapter.json",
                "summary": "work/agent_results/chapter-6.summary.json",
                "quiz": "work/agent_results/chapter-6.quiz.json",
                "card": "work/agent_results/chapter-6.card.json",
            }
        },
        "concept_pages": {},
    }

    integrate_node(state, cfg)
    result = await check_node(state, cfg)

    assert result["repair_targets"] == []


@pytest.mark.asyncio
async def test_check_accepts_custom_quiz_headings_when_quizblock_exists(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    result_dir.mkdir(parents=True)
    (result_dir / "chapter-6.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "title": "Point Estimation",
                    "body_md": (
                        "Opening explanation.\n\nClosing explanation.\n\n"
                        '<QuizBlock>\n<QuizItemSlot id="chapter-6:s0:slot-000" '
                        'topic="t" sourceRefs={["Week-10-p001"]} />\n</QuizBlock>'
                    ),
                    "concepts": [],
                    "citations": [],
                    "owner_task_id": "chapter-6:chapter",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.summary.json").write_text(
        json.dumps(
            {
                "result": {
                    "summary_md": "Summary.",
                    "citations": [],
                    "owner_task_id": "chapter-6:summary",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [
                        {
                            "question": "Q?",
                            "choices": ["A", "B"],
                            "answer": "A",
                            "explanation": "E.",
                            "citations": [],
                            "slot_id": "chapter-6:s0:slot-000",
                        }
                    ],
                    "owner_task_id": "chapter-6:quiz",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-6.card.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "items": [{"front": "Front", "back": "Back", "citations": []}],
                    "owner_task_id": "chapter-6:card",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "agent_results": {
            "chapter-6": {
                "chapter": "work/agent_results/chapter-6.chapter.json",
                "summary": "work/agent_results/chapter-6.summary.json",
                "quiz": "work/agent_results/chapter-6.quiz.json",
                "card": "work/agent_results/chapter-6.card.json",
            }
        },
        "concept_pages": {},
    }

    integrate_node(state, cfg)
    result = await check_node(state, cfg)

    assert result["repair_targets"] == []


@pytest.mark.asyncio
async def test_reconcile_node_runs_concept_extract_and_records_extract_outputs(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    result_dir.mkdir(parents=True)
    (result_dir / "chapter-1.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "title": "Search",
                    "body_md": "Body.",
                    "concepts": ["递归", "递推"],
                    "citations": [],
                    "owner_task_id": "chapter-1:chapter",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=TestLLMRuntime())
    state = {
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        }
    }

    result = await reconcile_node(state, cfg)

    concepts_path = book_dir / result["agent_results"]["chapter-1"]["concepts"]
    assert concepts_path.exists()
    concepts_payload = json.loads(concepts_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in concepts_payload["result"]["concepts"]] == ["递归", "递推"]
    reconciled = json.loads((book_dir / result["reconciled_concepts"]).read_text(encoding="utf-8"))
    assert set(reconciled["alias_map"]) >= {"递归", "递推"}


@pytest.mark.asyncio
async def test_concept_pages_node_passes_chapter_context_to_concept_agent(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    source_path = book_dir / "work" / "chapter_sources" / "chapter-1" / "source.md"
    concepts_path = book_dir / "work" / "concepts" / "reconciled.json"
    source_path.parent.mkdir(parents=True)
    concepts_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    source_path.write_text(
        "# Search\n\n<!-- source_ref: source-p001 -->\n\nRecursive search source.",
        encoding="utf-8",
    )
    concepts_path.write_text(
        json.dumps(
            {
                "concepts": [
                    {
                        "canonical": "递归",
                        "aliases": ["递推"],
                        "source_chapter_ids": ["chapter-1"],
                    }
                ],
                "alias_map": {"递推": "递归"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "title": "Search",
                    "body_md": "Chapter mentions recursion.",
                    "concepts": ["递归"],
                    "citations": [{"ref_id": "source-p001", "quote": "Recursive search source"}],
                    "owner_task_id": "chapter-1:chapter",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.summary.json").write_text(
        json.dumps({"result": {"summary_md": "Summary.", "owner_task_id": "chapter-1:summary"}}),
        encoding="utf-8",
    )
    runtime = RecordingRuntime(
        [
            {
                "name": "递归",
                "body_md": "递归 uses the source context.",
                "related": [],
                "citations": [{"ref_id": "source-p001", "quote": "Recursive search source"}],
                "owner_task_id": "concept:递归",
            }
        ]
    )
    cfg = BookConfig(book_dir=book_dir, book_id="book", title="Book", llm_runtime=runtime)
    cfg.notes_file.write_text(
        "English teaching: include English terms for every concept.",
        encoding="utf-8",
    )
    state = {
        "reconciled_concepts": "work/concepts/reconciled.json",
        "chapter_sources": {"chapter-1": "work/chapter_sources/chapter-1/source.md"},
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
            }
        },
    }

    await concept_pages_node(state, cfg)

    user_prompt = runtime.calls[0]["user"]
    assert "Recursive search source" in user_prompt
    assert "source-p001" in user_prompt
    assert "Chapter mentions recursion" in user_prompt
    assert "include English terms for every concept" in user_prompt


def test_integrate_uses_alias_map_embedded_in_reconciled_concepts(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    concept_dir = result_dir / "concepts"
    result_dir.mkdir(parents=True)
    concept_dir.mkdir(parents=True)
    reconciled_path = book_dir / "work" / "concepts" / "reconciled.json"
    reconciled_path.parent.mkdir(parents=True)
    reconciled_path.write_text(
        json.dumps(
            {
                "concepts": [
                    {"canonical": "递归", "aliases": ["递推"], "source_chapter_ids": ["chapter-1"]}
                ],
                "alias_map": {"递推": "递归"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (concept_dir / "递归.json").write_text(
        json.dumps({"name": "递归", "body_md": "Concept."}, ensure_ascii=False),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "title": "Search",
                    "body_md": "Compare [[递推]] and iteration.",
                    "concepts": ["递归"],
                    "citations": [],
                    "owner_task_id": "chapter-1:chapter",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.summary.json").write_text(
        json.dumps({"result": {"summary_md": "Summary.", "owner_task_id": "chapter-1:summary"}}),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "items": [
                        {
                            "question": "Q?",
                            "choices": ["A", "B"],
                            "answer": "A",
                            "explanation": "E.",
                            "slot_id": "chapter-1:s0:slot-000",
                        }
                    ],
                    "owner_task_id": "chapter-1:quiz",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.card.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "items": [{"front": "Front", "back": "Back"}],
                    "owner_task_id": "chapter-1:card",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "reconciled_concepts": "work/concepts/reconciled.json",
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        },
        "concept_pages": {"递归": "work/agent_results/concepts/递归.json"},
    }

    integrate_node(state, cfg)

    chapter = (book_dir / "content" / "docs" / "chapters" / "chapter-1.mdx").read_text(
        encoding="utf-8"
    )
    assert (
        '<PreviewLink href={"/docs/concepts/递归"} title={"递归"} '
        'summary={"Concept."}>递归</PreviewLink>'
        in chapter
    )
    assert "[[递推]]" not in chapter


@pytest.mark.asyncio
async def test_check_routes_unknown_refs_for_concept_and_quiz_owners(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    concept_dir = result_dir / "concepts"
    sources_dir = book_dir / "work" / "sources_md"
    result_dir.mkdir(parents=True)
    concept_dir.mkdir(parents=True)
    sources_dir.mkdir(parents=True)
    sources_dir.joinpath("source.md").write_text(
        "<!-- source_ref: source-p001 -->\nText.", encoding="utf-8"
    )
    (result_dir / "chapter-1.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "title": "Search",
                    "body_md": (
                        "Body.\n\n"
                        '<QuizBlock>\n<QuizItemSlot id="chapter-1:s0:slot-000" '
                        'topic="t" sourceRefs={["source-p001"]} />\n</QuizBlock>'
                    ),
                    "concepts": ["递归"],
                    "citations": [{"ref_id": "source-p001", "quote": "Text"}],
                    "owner_task_id": "chapter-1:chapter",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.summary.json").write_text(
        json.dumps({"result": {"summary_md": "Summary.", "owner_task_id": "chapter-1:summary"}}),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.quiz.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "items": [
                        {
                            "question": "Q?",
                            "choices": ["A", "B"],
                            "answer": "A",
                            "explanation": "E.",
                            "citations": [{"ref_id": "missing-p999", "quote": "Nope"}],
                            "slot_id": "chapter-1:s0:slot-000",
                        }
                    ],
                    "owner_task_id": "chapter-1:quiz",
                }
            }
        ),
        encoding="utf-8",
    )
    (result_dir / "chapter-1.card.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "items": [],
                    "owner_task_id": "chapter-1:card",
                }
            }
        ),
        encoding="utf-8",
    )
    (concept_dir / "递归.json").write_text(
        json.dumps(
            {
                "name": "递归",
                "body_md": "Concept.",
                "citations": [{"ref_id": "concept-missing-p999", "quote": "Nope"}],
                "owner_task_id": "concept:递归",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    state = {
        "sources_md": ["work/sources_md/source.md"],
        "agent_results": {
            "chapter-1": {
                "chapter": "work/agent_results/chapter-1.chapter.json",
                "summary": "work/agent_results/chapter-1.summary.json",
                "quiz": "work/agent_results/chapter-1.quiz.json",
                "card": "work/agent_results/chapter-1.card.json",
            }
        },
        "concept_pages": {"递归": "work/agent_results/concepts/递归.json"},
    }

    integrate_node(state, cfg)
    result = await check_node(state, cfg)

    assert result["repair_targets"] == ["chapter-1:quiz", "concept:递归"]

    repaired = await repair_node({**state, **result}, cfg)
    assert repaired["repair_targets"] == []
    integrate_node(state, cfg)
    passed = await check_node(state, cfg)
    assert passed["repair_targets"] == []


@pytest.mark.asyncio
async def test_check_node_aborts_when_mdx_validator_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bookwiki.pipeline.nodes.mdx_validator_available", lambda: False
    )
    cfg = BookConfig(
        book_dir=tmp_path / "book",
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )

    with pytest.raises(RuntimeError, match="mdx validator unavailable"):
        await check_node({}, cfg)


@pytest.mark.asyncio
async def test_check_node_escape_valve_degrades_to_error_log(
    tmp_path, monkeypatch, caplog
) -> None:
    import logging

    monkeypatch.setattr(
        "bookwiki.pipeline.nodes.mdx_validator_available", lambda: False
    )
    book_dir = tmp_path / "book"
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    cfg.generation["allowMissingMdxValidator"] = True

    with caplog.at_level(logging.ERROR, logger="bookwiki.pipeline.nodes"):
        result = await check_node({}, cfg)

    # Does not abort; the missing content index is still flagged the normal way.
    assert "repair_targets" in result
    assert any("allowMissingMdxValidator" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_check_node_installs_site_dependencies_without_node_modules(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("bookwiki.pipeline.nodes.mdx_validator_available", lambda: True)
    monkeypatch.setattr("bookwiki.pipeline.nodes.shutil.which", lambda name: "/usr/bin/pnpm")
    fake_template = tmp_path / "site-template"
    _write_minimal_site_template(fake_template)
    monkeypatch.setattr(site, "TEMPLATE_DIR", fake_template)
    book_dir = tmp_path / "book"
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    calls: list[dict[str, object]] = []

    def fake_run(cmd, *, cwd, env, check, capture_output, text, timeout):  # noqa: ANN001
        calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("bookwiki.pipeline.nodes.subprocess.run", fake_run)

    result = await check_node({}, cfg)

    assert result["repair_targets"] == []
    assert calls[0] == {"cmd": ["/usr/bin/pnpm", "install"], "cwd": cfg.site_dir, "timeout": 300}
    assert calls[1] == {
        "cmd": ["/usr/bin/pnpm", "run", "types:check"],
        "cwd": cfg.site_dir,
        "timeout": 120,
    }


@pytest.mark.asyncio
async def test_check_node_reports_site_dependency_install_failure(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("bookwiki.pipeline.nodes.mdx_validator_available", lambda: True)
    monkeypatch.setattr("bookwiki.pipeline.nodes.shutil.which", lambda name: "/usr/bin/pnpm")
    monkeypatch.setenv("BOOKWIKI_CHAT_API_KEY", "secret-token")
    fake_template = tmp_path / "site-template"
    _write_minimal_site_template(fake_template)
    monkeypatch.setattr(site, "TEMPLATE_DIR", fake_template)
    book_dir = tmp_path / "book"
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    monkeypatch.setattr(
        "bookwiki.pipeline.nodes.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="Install failed with secret-token"
        ),
    )

    result = await check_node({}, cfg)

    assert result["repair_targets"] == ["site:typecheck"]
    report = json.loads((book_dir / "work" / "logs" / "check-report.json").read_text())
    assert report["issues"][-1]["code"] == "SITE_TYPECHECK_ERROR"
    assert "site dependency install failed" in report["issues"][-1]["message"]
    assert "secret-token" not in report["issues"][-1]["message"]
    assert "[REDACTED]" in report["issues"][-1]["message"]


@pytest.mark.asyncio
async def test_check_node_runs_site_typecheck_when_dependencies_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("bookwiki.pipeline.nodes.mdx_validator_available", lambda: True)
    monkeypatch.setattr("bookwiki.pipeline.nodes.shutil.which", lambda name: "/usr/bin/pnpm")
    fake_template = tmp_path / "site-template"
    _write_minimal_site_template(fake_template)
    monkeypatch.setattr(site, "TEMPLATE_DIR", fake_template)
    book_dir = tmp_path / "book"
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        language="en-US",
        llm_runtime=TestLLMRuntime(),
    )
    calls: list[dict[str, object]] = []

    def fake_run(cmd, *, cwd, env, check, capture_output, text, timeout):  # noqa: ANN001
        calls.append(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "check": check,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            }
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("bookwiki.pipeline.nodes.subprocess.run", fake_run)

    result = await check_node({}, cfg)

    assert result["repair_targets"] == []
    assert calls[0]["cmd"] == ["/usr/bin/pnpm", "install"]
    assert calls[0]["cwd"] == cfg.site_dir
    assert calls[0]["env"]["BOOKWIKI_SITE_LANGUAGE"] == "en-US"
    assert calls[0]["env"]["NODE_OPTIONS"] == "--max-old-space-size=4096"
    assert "BOOKWIKI_CHAT_API_KEY" not in calls[0]["env"]
    assert calls[0]["timeout"] == 300
    assert calls[1]["cmd"] == ["/usr/bin/pnpm", "run", "types:check"]
    assert calls[1]["cwd"] == cfg.site_dir
    assert calls[1]["timeout"] == 120
    assert not (cfg.site_dir / "node_modules").is_symlink()


@pytest.mark.asyncio
async def test_check_node_reports_site_typecheck_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bookwiki.pipeline.nodes.mdx_validator_available", lambda: True)
    monkeypatch.setattr("bookwiki.pipeline.nodes.shutil.which", lambda name: "/usr/bin/pnpm")
    monkeypatch.setenv("BOOKWIKI_CHAT_API_KEY", "secret-token")
    fake_template = tmp_path / "site-template"
    _write_minimal_site_template(fake_template)
    (fake_template / "node_modules").mkdir()
    monkeypatch.setattr(site, "TEMPLATE_DIR", fake_template)
    book_dir = tmp_path / "book"
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        if cmd == ["/usr/bin/pnpm", "install"]:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot find module with secret-token",
        )

    monkeypatch.setattr("bookwiki.pipeline.nodes.subprocess.run", fake_run)

    result = await check_node({}, cfg)

    assert result["repair_targets"] == ["site:typecheck"]
    report = json.loads((book_dir / "work" / "logs" / "check-report.json").read_text())
    assert report["issues"][-1]["code"] == "SITE_TYPECHECK_ERROR"
    assert "Cannot find module" in report["issues"][-1]["message"]
    assert "secret-token" not in report["issues"][-1]["message"]
    assert "[REDACTED]" in report["issues"][-1]["message"]


@pytest.mark.asyncio
async def test_check_node_removes_symlinked_site_node_modules_before_install(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("bookwiki.pipeline.nodes.mdx_validator_available", lambda: True)
    monkeypatch.setattr("bookwiki.pipeline.nodes.shutil.which", lambda name: "/usr/bin/pnpm")
    fake_template = tmp_path / "site-template"
    _write_minimal_site_template(fake_template)
    template_node_modules = fake_template / "node_modules"
    template_node_modules.mkdir()
    monkeypatch.setattr(site, "TEMPLATE_DIR", fake_template)
    book_dir = tmp_path / "book"
    content_dir = book_dir / "content" / "docs"
    content_dir.mkdir(parents=True)
    (content_dir / "index.mdx").write_text("---\ntitle: Mini\n---\n\n# Mini\n", encoding="utf-8")
    (content_dir / "meta.json").write_text('{"pages":["index"]}', encoding="utf-8")
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    cfg.site_dir.mkdir(parents=True)
    (cfg.site_dir / "node_modules").symlink_to(template_node_modules, target_is_directory=True)
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("bookwiki.pipeline.nodes.subprocess.run", fake_run)

    result = await check_node({}, cfg)

    assert result["repair_targets"] == []
    assert calls == [["/usr/bin/pnpm", "install"], ["/usr/bin/pnpm", "run", "types:check"]]
    assert not (cfg.site_dir / "node_modules").is_symlink()


@pytest.mark.asyncio
async def test_repair_node_records_exhausted_targets_loudly(tmp_path, caplog) -> None:
    import logging

    book_dir = tmp_path / "book"
    logs_dir = book_dir / "work" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "check-report.json").write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "severity": "error",
                        "code": "MDX_PARSE_ERROR",
                        "message": "chapter-1.mdx fails MDX compilation: boom",
                        "owner_task_id": "chapter-1:chapter",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = BookConfig(
        book_dir=book_dir,
        book_id="book",
        title="Book",
        llm_runtime=TestLLMRuntime(),
    )
    # The target has already consumed maxRepairRounds (default 3): the next pass must
    # NOT silently drop it - it must record the exhaustion.
    state = {
        "repair_targets": ["chapter-1:chapter"],
        "_repair_rounds": {"chapter-1:chapter": 3},
        "check_report": "work/logs/check-report.json",
    }

    with caplog.at_level(logging.WARNING, logger="bookwiki.pipeline.nodes"):
        result = await repair_node(state, cfg)

    assert result["repairs"] == []
    assert result["repair_exhausted"] == [
        {"owner_task_id": "chapter-1:chapter", "codes": ["MDX_PARSE_ERROR"], "rounds": 3}
    ]
    exhausted_file = json.loads((logs_dir / "repair-exhausted.json").read_text(encoding="utf-8"))
    assert exhausted_file["exhausted"][0]["owner_task_id"] == "chapter-1:chapter"
    assert any("repair exhausted" in record.getMessage() for record in caplog.records)
