from __future__ import annotations

import json

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
from tests.fakes import RecordingRuntime


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
async def test_check_routes_bad_quiz_answer_and_repair_rewrites_agent_result(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    result_dir.mkdir(parents=True)
    (result_dir / "chapter-1.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-1",
                    "title": "Search",
                    "body_md": "Search uses [[递推]].",
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
                            "question": "What is search?",
                            "choices": ["A", "B"],
                            "answer": "C",
                            "explanation": "Because.",
                            "citations": [{"ref_id": "source-p001", "quote": "Search source"}],
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
    checked = check_node(state, cfg)

    assert checked["repair_targets"] == ["chapter-1:quiz"]
    report = json.loads((book_dir / "work" / "logs" / "check-report.json").read_text())
    assert report["issues"][0]["code"] == "QUIZ_ANSWER_NOT_IN_CHOICES"
    assert report["issues"][0]["owner_task_id"] == "chapter-1:quiz"

    repaired = await repair_node({**state, **checked}, cfg)
    assert repaired["repair_targets"] == []
    repaired_payload = json.loads(quiz_path.read_text(encoding="utf-8"))
    assert repaired_payload["result"]["items"][0]["answer"] == "A"

    integrate_node(state, cfg)
    passed = check_node(state, cfg)
    assert passed["repair_targets"] == []


def test_check_accepts_hyphenated_source_refs_from_sources_md(tmp_path) -> None:
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
                    "body_md": "Body.",
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
    result = check_node(state, cfg)

    assert result["repair_targets"] == []


def test_check_accepts_custom_quiz_headings_when_quizblock_exists(tmp_path) -> None:
    book_dir = tmp_path / "book"
    result_dir = book_dir / "work" / "agent_results"
    result_dir.mkdir(parents=True)
    (result_dir / "chapter-6.chapter.json").write_text(
        json.dumps(
            {
                "result": {
                    "chapter_id": "chapter-6",
                    "title": "Point Estimation",
                    "body_md": "Opening explanation.\n\nClosing explanation.",
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
                        }
                    ],
                    "placements": [
                        {"after_block": 0, "item_indexes": [1], "title": "Checkpoint"}
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
    result = check_node(state, cfg)

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
    assert "[递归](../concepts/递归)" in chapter
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
                    "body_md": "Body.",
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
    result = check_node(state, cfg)

    assert result["repair_targets"] == ["chapter-1:quiz", "concept:递归"]

    repaired = await repair_node({**state, **result}, cfg)
    assert repaired["repair_targets"] == []
    integrate_node(state, cfg)
    passed = check_node(state, cfg)
    assert passed["repair_targets"] == []
