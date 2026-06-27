from __future__ import annotations

from bookwiki.integrator.exam_renderer import render_exam_mdx
from bookwiki.schemas.quiz import ExamResult

PAPER = ExamResult(
    chapter_id="ch01",
    owner_task_id="ch01:exam",
    questions=[
        {
            "type": "single_choice",
            "id": "ex-1",
            "question": "What does A* minimise?",
            "options": ["$f(n)$", "$g(n)$"],
            "answer": ["$f(n)$"],
            "explanation": "A* expands by $f = g + h$.",
            "from_exam": True,
            "source_refs": ["src-p001"],
        },
        {
            "type": "fill_blank",
            "id": "ex-2",
            "question": "A* uses $f(n) =$ ___ $+$ ___.",
            "accepted_answers": [["$g(n)$"], ["$h(n)$"]],
            "source_refs": ["src-p001"],
        },
        {
            "type": "worked",
            "id": "ex-3",
            "question": "Prove A* is optimal.",
            "reference_answer": "Assume an admissible $h$; then ...",
            "rubric": [{"point": "states admissibility", "weight": 2.0}],
            "explanation": "Tests optimality reasoning.",
            "source_refs": ["src-p001"],
        },
    ],
)


def test_renders_exam_block_wrapper() -> None:
    mdx = render_exam_mdx(PAPER, mode="exam")

    assert mdx.startswith('<ExamBlock chapterId="ch01" mode="exam">')
    assert mdx.rstrip().endswith("</ExamBlock>")


def test_renders_single_choice_with_answer_ids() -> None:
    mdx = render_exam_mdx(PAPER, mode="exam")

    # Answer is rendered as choice ids, not raw option text.
    assert '<ExamItem id="ex-1" type="single_choice" answer={["choice-1"]} fromExam>' in mdx
    assert '<ExamChoice id="choice-1">' in mdx
    assert "$f(n)$" in mdx


def test_renders_fill_blank_accepted_answers() -> None:
    mdx = render_exam_mdx(PAPER, mode="exam")

    expected = '<ExamItem id="ex-2" type="fill_blank" acceptedAnswers={[["$g(n)$"], ["$h(n)$"]]}>'
    assert expected in mdx


def test_renders_worked_reference_and_rubric() -> None:
    mdx = render_exam_mdx(PAPER, mode="exam")

    assert 'rubric={[{"point": "states admissibility", "weight": 2.0}]}' in mdx
    assert 'referenceAnswer={"Assume an admissible $h$; then ..."}' in mdx


def test_walkthrough_mode_renders_concept_recap() -> None:
    paper = PAPER.model_copy(deep=True)
    paper.questions[2].concept_recap_md = "梯度/最优性的基本定义。"

    mdx = render_exam_mdx(paper, mode="walkthrough")

    assert 'mode="walkthrough"' in mdx
    assert "<ExamConceptRecap>" in mdx
    assert "梯度/最优性的基本定义。" in mdx
