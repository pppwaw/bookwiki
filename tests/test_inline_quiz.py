from __future__ import annotations

import pytest

from bookwiki.checkers.quiz_extractor import extract_inline_quizzes
from bookwiki.generate.inline_quiz import (
    sanitize_inline_quizzes,
    strip_inline_quizzes_and_control_slots,
)
from bookwiki.pipeline.nodes import (
    _drop_invalid_inline_quiz_items,
    _inline_quiz_answer_issues,
    _resolve_item_slots,
)

KNOWLEDGE_BLOCK = """## 反相放大器

讲解 $v_o = -\\frac{R_f}{R_1} v_i$。

<QuizBlock>
<QuizItem id="quiz-001" answer="choice-1" citations={[{ ref_id: "p1", quote: "q" }]}>
<QuizQuestion>
虚短指什么？
</QuizQuestion>
<QuizChoices>
<QuizChoice id="choice-1">
电压相等
</QuizChoice>
<QuizChoice id="choice-2">
电流相等
</QuizChoice>
</QuizChoices>
<QuizCheck />
<QuizExplanation>
$v_1 = v_2$。
</QuizExplanation>
</QuizItem>
</QuizBlock>
"""


def test_extractor_parses_item_choices_and_citations() -> None:
    blocks = extract_inline_quizzes(KNOWLEDGE_BLOCK)
    assert len(blocks) == 1
    item = blocks[0]["children"][0]
    assert item["kind"] == "item"
    assert item["answer"] == "choice-1"
    assert [c["id"] for c in item["choices"]] == ["choice-1", "choice-2"]
    assert item["citations"]["ok"] is True
    assert item["citations"]["value"] == [{"ref_id": "p1", "quote": "q"}]


def test_extractor_rejects_unsafe_expression() -> None:
    body = '<QuizBlock>\n<QuizItemSlot id="bad" topic="t" sourceRefs={someVar} />\n</QuizBlock>'
    slot = extract_inline_quizzes(body)[0]["children"][0]
    assert slot["kind"] == "slot"
    assert slot["sourceRefs"]["ok"] is False


def test_sanitize_keeps_valid_item_and_grounds_citation() -> None:
    res = sanitize_inline_quizzes(
        KNOWLEDGE_BLOCK, allowed_refs={"p1"}, chapter_id="ch", section_index=0
    )
    assert "<QuizItem " in res.body_md
    assert 'answer="choice-1"' in res.body_md
    assert '"ref_id": "p1"' in res.body_md


def test_sanitize_drops_ungrounded_citation_but_keeps_item() -> None:
    res = sanitize_inline_quizzes(
        KNOWLEDGE_BLOCK, allowed_refs=set(), chapter_id="ch", section_index=0
    )
    assert "<QuizItem " in res.body_md
    assert "citations={[]}" in res.body_md


def test_sanitize_drops_item_with_answer_not_in_choices_and_removes_empty_block() -> None:
    body = KNOWLEDGE_BLOCK.replace('answer="choice-1"', 'answer="choice-9"')
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=0)
    assert "<QuizBlock>" not in res.body_md
    assert any("invalid quiz item" in w for w in res.warnings)


def test_sanitize_assigns_canonical_slot_id_and_drops_ungrounded_slot() -> None:
    body = (
        '<QuizBlock>\n<QuizItemSlot id="auto" topic="计算" sourceRefs={["p1"]} />\n'
        '<QuizItemSlot id="auto" topic="bad" sourceRefs={["unknown"]} />\n</QuizBlock>'
    )
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=2)
    assert len(res.slot_specs) == 1
    assert res.slot_specs[0].slot_id == "ch:s2:slot-000"
    assert 'id="ch:s2:slot-000"' in res.body_md


def test_sanitize_dedupes_identical_slots() -> None:
    body = (
        '<QuizBlock>\n<QuizItemSlot id="auto" topic="t" sourceRefs={["p1"]} />\n'
        '<QuizItemSlot id="auto" topic="t" sourceRefs={["p1"]} />\n</QuizBlock>'
    )
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=0)
    assert len(res.slot_specs) == 1


def test_sanitize_caps_items_per_block() -> None:
    slots = "\n".join(
        f'<QuizItemSlot id="auto" topic="t{i}" sourceRefs={{["p1"]}} />' for i in range(10)
    )
    body = f"<QuizBlock>\n{slots}\n</QuizBlock>"
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=0)
    # soft target 3, hard cap 6 per block
    assert len(res.slot_specs) == 6


def test_sanitize_best_effort_on_unparseable_body() -> None:
    body = "当 n<30 时使用 t 分布。"
    res = sanitize_inline_quizzes(body, allowed_refs=set(), chapter_id="ch", section_index=0)
    assert res.body_md == body
    assert res.slot_specs == []


def test_sanitize_rescues_application_slot_from_unparseable_body() -> None:
    # ``n<30`` makes the body non-MDX-parseable; the application slot must still be rescued
    # by the regex fallback (not silently dropped) and stamped with a canonical id.
    body = (
        "当 n<30 时使用 t 分布。\n\n"
        "<QuizBlock>\n"
        '<QuizItemSlot id="auto" topic="计算样本量" sourceRefs={["p1"]} />\n'
        "</QuizBlock>"
    )
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=0)
    assert len(res.slot_specs) == 1
    assert res.slot_specs[0].slot_id == "ch:s0:slot-000"
    assert res.slot_specs[0].topic == "计算样本量"
    assert res.slot_specs[0].source_refs == ["p1"]
    assert 'id="ch:s0:slot-000"' in res.body_md
    assert "当 n<30 时使用 t 分布。" in res.body_md  # unparseable prose preserved for chapter heal


def test_sanitize_fallback_drops_ungrounded_slot_from_unparseable_body() -> None:
    body = (
        '当 n<30 时使用 t 分布。\n\n<QuizItemSlot id="auto" topic="x" sourceRefs={["unknown"]} />'
    )
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=0)
    assert res.slot_specs == []
    assert "<QuizItemSlot" not in res.body_md
    assert any("invalid quiz slot" in w for w in res.warnings)


def test_strip_removes_blocks_and_slots_keeps_prose() -> None:
    body = "正文。\n\n" + KNOWLEDGE_BLOCK + "\n更多。"
    stripped = strip_inline_quizzes_and_control_slots(body)
    assert "<QuizBlock>" not in stripped
    assert "<QuizItem" not in stripped
    assert "正文。" in stripped
    assert "更多。" in stripped


def test_resolve_item_slots_fills_and_removes_unfilled_block() -> None:
    body = (
        "## 应用\n\n<QuizBlock>\n"
        '<QuizItemSlot id="ch:s0:slot-000" topic="t" sourceRefs={["p1"]} />\n'
        "</QuizBlock>\n\n<QuizBlock>\n"
        '<QuizItemSlot id="ch:s0:slot-999" topic="t" sourceRefs={["p1"]} />\n'
        "</QuizBlock>"
    )
    quiz = {
        "items": [
            {
                "slot_id": "ch:s0:slot-000",
                "question": "Q",
                "choices": ["a", "b"],
                "answer": "a",
                "explanation": "e",
                "citations": [],
            }
        ]
    }
    out = _resolve_item_slots(body, quiz)
    assert "<QuizItem " in out
    assert "slot-999" not in out
    assert out.count("<QuizBlock>") == 1


def test_resolve_item_slots_expression_string_props_remain_extractable() -> None:
    body = (
        "<QuizBlock>\n"
        '<QuizItemSlot id="ch:s0:slot-000" topic="t" sourceRefs={["p1"]} />\n'
        '<QuizItemSlot id="ch:s0:slot-001" topic="t" sourceRefs={["p1"]} />\n'
        "</QuizBlock>"
    )
    quiz = {
        "items": [
            {
                "slot_id": "ch:s0:slot-000",
                "question": "Q1",
                "choices": ["a", "b"],
                "answer": "a",
                "explanation": "e",
                "citations": [],
            },
            {
                "slot_id": "ch:s0:slot-001",
                "question": "Q2",
                "choices": ["a", "b"],
                "answer": "c",
                "explanation": "e",
                "citations": [],
            },
        ]
    }

    blocks = extract_inline_quizzes(_resolve_item_slots(body, quiz))

    items = blocks[0]["children"]
    assert items[0]["answer"] == "choice-1"
    assert items[1]["answer"] == "invalid-answer-002"
    assert [choice["id"] for choice in items[0]["choices"]] == ["choice-1", "choice-2"]


def test_resolve_item_slots_fail_loud_on_slotless_item() -> None:
    quiz = {"items": [{"question": "Q", "choices": ["a", "b"], "answer": "a", "explanation": "e"}]}
    with pytest.raises(ValueError, match="no slot_id"):
        _resolve_item_slots("body", quiz)


def test_inline_quiz_answer_issues_warns_on_bad_answer() -> None:
    issues = _inline_quiz_answer_issues(
        KNOWLEDGE_BLOCK.replace('answer="choice-1"', 'answer="choice-9"'), "chapter-1"
    )
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].code == "INLINE_QUIZ_ANSWER_NOT_IN_CHOICES"
    assert issues[0].owner_task_id == "chapter-1:quiz"


def test_inline_quiz_answer_issues_clean_on_valid_answer() -> None:
    assert _inline_quiz_answer_issues(KNOWLEDGE_BLOCK, "chapter-1") == []


def test_drop_invalid_inline_quiz_items_removes_bad_item_only() -> None:
    bad = KNOWLEDGE_BLOCK.replace('answer="choice-1"', 'answer="choice-9"')
    body = f"前文。\n\n{bad}\n后文。"

    out = _drop_invalid_inline_quiz_items(body)

    assert "前文。" in out
    assert "后文。" in out
    assert "<QuizItem" not in out
    assert "<QuizBlock>" not in out
    assert _inline_quiz_answer_issues(out, "chapter-1") == []


def test_drop_invalid_inline_quiz_items_keeps_valid_item() -> None:
    out = _drop_invalid_inline_quiz_items(KNOWLEDGE_BLOCK)

    assert "<QuizItem" in out
    assert 'answer="choice-1"' in out


def test_sanitize_offsets_survive_non_bmp_chars_before_block() -> None:
    # Non-BMP chars (𝑋 U+1D44B, 𝜇 U+1D707) are 2 UTF-16 units but 1 code point. The Node
    # extractor must return code-point offsets so the Python slice does not drift and corrupt
    # the body (regression: a drifted slice produced "<<QuizBlock>").
    body = (
        "估计量 𝑋 与 𝜇 的说明。\n\n"
        '<QuizBlock>\n<QuizItemSlot id="auto" topic="t" sourceRefs={["p1"]} />\n</QuizBlock>\n'
    )
    res = sanitize_inline_quizzes(body, allowed_refs={"p1"}, chapter_id="ch", section_index=0)
    assert "<<" not in res.body_md
    assert "𝑋" in res.body_md and "𝜇" in res.body_md
    assert [s.slot_id for s in res.slot_specs] == ["ch:s0:slot-000"]


def test_strip_offsets_survive_non_bmp_chars_before_block() -> None:
    body = "公式 𝑋 推导。\n\n" + KNOWLEDGE_BLOCK + "\n结尾文本。"
    stripped = strip_inline_quizzes_and_control_slots(body)
    assert "<QuizBlock>" not in stripped
    assert "<QuizItem" not in stripped
    assert "公式 𝑋 推导。" in stripped
    assert "结尾文本。" in stripped
