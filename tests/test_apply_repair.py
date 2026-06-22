from __future__ import annotations

from bookwiki.pipeline.nodes import (
    _drop_empty_cards,
    _drop_invalid_citations,
    _drop_invalid_quiz_items,
)
from bookwiki.schemas.card import CardResult
from bookwiki.schemas.quiz import QuizResult


def test_drop_invalid_citations_removes_unverifiable_refs_not_collapse() -> None:
    result = {
        "chapter_id": "chapter-1",
        "citations": [
            {"ref_id": "good-p001", "quote": "kept"},
            {"ref_id": "bad-p999", "quote": "drop me"},
            {"ref_id": "good-p002", "quote": "also kept"},
            {"ref_id": "bad-p888", "quote": "drop me too"},
        ],
        "items": [
            {"text": "x", "citations": [{"ref_id": "bad-p777", "quote": "nested drop"}]},
        ],
    }
    allowed = {"good-p001", "good-p002"}

    removed = _drop_invalid_citations(result, allowed)

    # Invalid refs are DROPPED, not collapsed onto a single valid ref.
    assert sorted(removed) == ["bad-p777", "bad-p888", "bad-p999"]
    assert [c["ref_id"] for c in result["citations"]] == ["good-p001", "good-p002"]
    assert result["items"][0]["citations"] == []
    # No invalid ref was reassigned to a (wrong) valid ref.
    remaining = {c["ref_id"] for c in result["citations"]}
    assert remaining <= allowed


def test_drop_invalid_quiz_items_removes_item() -> None:
    result = {
        "chapter_id": "chapter-1",
        "items": [
            {"question": "Q1", "choices": ["A", "B"], "answer": "A", "explanation": "e"},
            {"question": "Q2-bad", "choices": ["A", "B"], "answer": "C", "explanation": "e"},
            {"question": "Q3", "choices": ["A", "B"], "answer": "B", "explanation": "e"},
        ],
        "owner_task_id": "chapter-1:quiz",
    }

    removed = _drop_invalid_quiz_items(result)

    assert removed == ["Q2-bad"]
    assert [item["question"] for item in result["items"]] == ["Q1", "Q3"]
    # The repaired result still validates against the schema.
    QuizResult.model_validate(result)


def test_drop_empty_cards_removes_blank_sides_no_stub_text() -> None:
    result = {
        "chapter_id": "chapter-1",
        "items": [
            {"front": "Q", "back": "A", "citations": []},
            {"front": "", "back": "orphan", "citations": []},
            {"front": "orphan", "back": "   ", "citations": []},
        ],
        "owner_task_id": "chapter-1:card",
    }

    removed = _drop_empty_cards(result)

    assert removed == ["card 2", "card 3"]
    assert [item["front"] for item in result["items"]] == ["Q"]
    # No fabricated placeholder text leaks into the artifact.
    serialized = str(result)
    assert "Review the source material" not in serialized
    CardResult.model_validate(result)
