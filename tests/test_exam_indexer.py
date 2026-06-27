from __future__ import annotations

from pathlib import Path

from bookwiki.indexer.mdx_parser import parse_mdx_file
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
            "source_refs": ["src-p001"],
        },
    ],
)


def _write_exam_page(tmp_path: Path) -> Path:
    chapters = tmp_path / "chapters" / "ch01"
    chapters.mkdir(parents=True)
    page = chapters / "exam.mdx"
    page.write_text(
        "---\ntitle: Exam\ntype: chapter\nchapter_id: ch01\n---\n\n"
        + render_exam_mdx(PAPER, mode="exam"),
        encoding="utf-8",
    )
    return page


def test_parses_all_exam_item_types(tmp_path: Path) -> None:
    page = parse_mdx_file(_write_exam_page(tmp_path), root=tmp_path)

    by_id = {item["id"]: item for item in page.quiz_items}
    assert set(by_id) == {"ex-1", "ex-2", "ex-3"}
    assert [by_id[i]["type"] for i in ("ex-1", "ex-2", "ex-3")] == [
        "single_choice",
        "fill_blank",
        "worked",
    ]


def test_choice_answer_maps_back_to_option_text(tmp_path: Path) -> None:
    page = parse_mdx_file(_write_exam_page(tmp_path), root=tmp_path)
    item = next(item for item in page.quiz_items if item["id"] == "ex-1")

    assert item["choices"] == ["$f(n)$", "$g(n)$"]
    assert item["answer"] == "$f(n)$"
    assert item["from_exam"] is True


def test_fill_blank_grading_json_carries_accepted_answers(tmp_path: Path) -> None:
    page = parse_mdx_file(_write_exam_page(tmp_path), root=tmp_path)
    item = next(item for item in page.quiz_items if item["id"] == "ex-2")

    assert item["grading_json"] == {"accepted_answers": [["$g(n)$"], ["$h(n)$"]]}


def test_worked_grading_json_carries_reference_and_rubric(tmp_path: Path) -> None:
    page = parse_mdx_file(_write_exam_page(tmp_path), root=tmp_path)
    item = next(item for item in page.quiz_items if item["id"] == "ex-3")

    assert item["reference_answer"].startswith("Assume an admissible")
    assert item["grading_json"]["rubric"] == [{"point": "states admissibility", "weight": 2.0}]
