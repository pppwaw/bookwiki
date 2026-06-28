from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class MdxPage:
    id: str
    slug: str
    path: Path
    relative_path: str
    title: str
    type: str
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    chapter_id: str | None = None
    order_index: int | None = None
    quiz_items: list[dict[str, Any]] = field(default_factory=list)
    card_items: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)


def parse_mdx_file(path: str | Path, root: str | Path | None = None) -> MdxPage:
    path = Path(path)
    root_path = Path(root) if root is not None else path.parent
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    relative_path = path.relative_to(root_path).as_posix()
    slug = _slug_from_relative_path(relative_path)
    page_id = slug or "index"
    quiz_items = _component_items(body, "QuizBlock", "items")
    quiz_items.extend(_worked_child_items(body))
    quiz_items.extend(_exam_child_items(body))
    card_items = _component_items(body, "AnkiDeck", "cards")
    refs = _source_refs(frontmatter, body, quiz_items, card_items)
    page_type = str(frontmatter.get("type") or _infer_type(relative_path))

    return MdxPage(
        id=page_id,
        slug=slug or "index",
        path=path,
        relative_path=relative_path,
        title=str(frontmatter.get("title") or _first_heading(body) or path.stem),
        type=page_type,
        body=body.strip(),
        frontmatter=frontmatter,
        chapter_id=_optional_str(frontmatter.get("chapter_id")),
        order_index=_optional_int(frontmatter.get("order_index")),
        quiz_items=quiz_items,
        card_items=card_items,
        source_refs=refs,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            payload = yaml.safe_load(raw) if raw.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
            return payload, body
    return {}, text


def _slug_from_relative_path(relative_path: str) -> str:
    value = Path(relative_path).with_suffix("").as_posix()
    if value == "index":
        return "index"
    if value.endswith("/index"):
        return value[: -len("/index")]
    return value


def _infer_type(relative_path: str) -> str:
    first_part = relative_path.split("/", 1)[0]
    if first_part == "chapters":
        return "chapter"
    if first_part == "concepts":
        return "concept"
    if first_part == "sources":
        return "source"
    return "index"


def _first_heading(body: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


# A tag's attribute span up to its closing `>` / `/>`, skipping over quoted values so a literal
# `>` inside an attribute (e.g. ``topic="... t>0"`` or a JSON prop ``items={[{"q":"n>1"}]}``)
# cannot end the tag early. A plain ``[^>]*`` truncates at that `>` and the tag fails to match,
# silently dropping the item from the index. Non-greedy + quote-first so an inner `>` is consumed.
_TAG_ATTRS = r"""(?:"[^"]*"|'[^']*'|[^>])*?"""


def _component_items(body: str, component: str, prop: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in re.finditer(rf"<{re.escape(component)}\b{_TAG_ATTRS}>", body, flags=re.DOTALL):
        value = _extract_braced_prop(match.group(0), prop)
        if value is None:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            items.extend(item for item in parsed if isinstance(item, dict))
    if component == "QuizBlock":
        items.extend(_quiz_child_items(body))
    elif component == "AnkiDeck":
        items.extend(_anki_child_items(body))
    return items


def _quiz_child_items(body: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for block in _component_blocks(body, "QuizBlock"):
        for item_block in _component_blocks(block["body"], "QuizItem"):
            choices = []
            answer_id = _prop_value(item_block["attrs"], "answer")
            answer = answer_id or ""
            for choice_block in _component_blocks(item_block["body"], "QuizChoice"):
                choice_id = _prop_value(choice_block["attrs"], "id") or ""
                choice_text = _clean_child_text(choice_block["body"])
                choices.append(choice_text)
                if choice_id and choice_id == answer_id:
                    answer = choice_text
            items.append(
                {
                    "id": _prop_value(item_block["attrs"], "id") or "",
                    "question": _first_child_text(item_block["body"], "QuizQuestion"),
                    "choices": choices,
                    "answer": answer,
                    "answer_id": answer_id,
                    "explanation": _first_child_text(item_block["body"], "QuizExplanation"),
                    "citations": _prop_json(item_block["attrs"], "citations", default=[]),
                }
            )
    return items


def _worked_child_items(body: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item_block in _component_blocks(body, "WorkedProblem"):
        reference_answer = _prop_value(item_block["attrs"], "referenceAnswer") or ""
        rubric = _prop_json(item_block["attrs"], "rubric", default=[])
        items.append(
            {
                "id": _prop_value(item_block["attrs"], "id") or "",
                "type": "worked",
                "question": _prop_value(item_block["attrs"], "question") or "",
                "answer": reference_answer,
                "reference_answer": reference_answer,
                "rubric": rubric,
                "explanation": _prop_value(item_block["attrs"], "explanation") or "",
                "citations": _prop_json(item_block["attrs"], "citations", default=[]),
                "grading_json": {"reference_answer": reference_answer, "rubric": rubric},
            }
        )
    return items


def _exam_child_items(body: str) -> list[dict[str, Any]]:
    """Parse ``<ExamBlock>`` / ``<ExamItem>`` (chapter exam + paper walkthrough) into quiz items.

    The exam grammar is a parallel surface to ``<QuizBlock>``: ``type`` distinguishes
    single/multiple_choice, fill_blank, and worked. Choice answers are stored as choice ids in
    the MDX and mapped back to option text here; fill_blank / worked carry their grading data in
    ``grading_json`` so the site can judge them the same way it judges legacy quizzes.
    """

    items: list[dict[str, Any]] = []
    for block in _component_blocks(body, "ExamBlock"):
        for item_block in _component_blocks(block["body"], "ExamItem"):
            attrs = item_block["attrs"]
            inner = item_block["body"]
            kind = _prop_value(attrs, "type") or "single_choice"
            item: dict[str, Any] = {
                "id": _prop_value(attrs, "id") or "",
                "type": kind,
                "question": _first_child_text(inner, "ExamQuestion"),
                "explanation": _first_child_text(inner, "ExamExplanation"),
                "concept_recap_md": _first_child_text(inner, "ExamConceptRecap"),
                "from_exam": re.search(r"\bfromExam\b", attrs) is not None,
                "source_refs": [],
            }
            if kind in {"single_choice", "multiple_choice"}:
                _fill_exam_choice(item, attrs, inner)
            elif kind == "fill_blank":
                accepted = _prop_json(attrs, "acceptedAnswers", default=[])
                item["answer"] = ""
                item["grading_json"] = {"accepted_answers": accepted}
            elif kind == "worked":
                reference = _prop_value(attrs, "referenceAnswer") or ""
                rubric = _prop_json(attrs, "rubric", default=[])
                item["answer"] = reference
                item["reference_answer"] = reference
                item["rubric"] = rubric
                item["grading_json"] = {"reference_answer": reference, "rubric": rubric}
            items.append(item)
    return items


def _fill_exam_choice(item: dict[str, Any], attrs: str, inner: str) -> None:
    answer_ids = _prop_json(attrs, "answer", default=[])
    if not isinstance(answer_ids, list):
        answer_ids = []
    choices: list[str] = []
    answers: list[str] = []
    for choice_block in _component_blocks(inner, "ExamChoice"):
        choice_id = _prop_value(choice_block["attrs"], "id") or ""
        choice_text = _clean_child_text(choice_block["body"])
        choices.append(choice_text)
        if choice_id in answer_ids:
            answers.append(choice_text)
    item["choices"] = choices
    item["answer"] = answers[0] if len(answers) == 1 else ", ".join(answers)
    item["answer_list"] = answers


def _anki_child_items(body: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for block in _component_blocks(body, "AnkiDeck"):
        for card_block in _component_blocks(block["body"], "AnkiCard"):
            items.append(
                {
                    "id": _prop_value(card_block["attrs"], "id") or "",
                    "front": _first_child_text(card_block["body"], "AnkiFront"),
                    "back": _first_child_text(card_block["body"], "AnkiBack"),
                    "citations": _prop_json(card_block["attrs"], "citations", default=[]),
                }
            )
    return items


def _component_blocks(body: str, component: str) -> list[dict[str, str]]:
    pattern = re.compile(
        rf"<{re.escape(component)}(?P<attrs>\s{_TAG_ATTRS})?>(?P<body>[\s\S]*?)</{re.escape(component)}>",
        flags=re.DOTALL,
    )
    return [
        {"attrs": match.group("attrs") or "", "body": match.group("body")}
        for match in pattern.finditer(body)
    ]


def _first_child_text(body: str, component: str) -> str:
    blocks = _component_blocks(body, component)
    if not blocks:
        return ""
    return _clean_child_text(blocks[0]["body"])


def _clean_child_text(value: str) -> str:
    return textwrap.dedent(value).strip()


def _prop_value(attrs: str, prop: str) -> str | None:
    braced = _extract_braced_prop(attrs, prop)
    if braced is not None:
        try:
            value = json.loads(braced)
        except json.JSONDecodeError:
            return braced.strip()
        return str(value) if value is not None else None
    match = re.search(rf"\b{re.escape(prop)}=(['\"])(.*?)\1", attrs, flags=re.DOTALL)
    return match.group(2) if match else None


def _prop_json(attrs: str, prop: str, default: Any) -> Any:
    value = _extract_braced_prop(attrs, prop)
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _extract_braced_prop(tag: str, prop: str) -> str | None:
    marker = f"{prop}={{"
    marker_index = tag.find(marker)
    if marker_index < 0:
        return None
    index = marker_index + len(marker)
    depth = 1
    quote: str | None = None
    escaped = False
    chars: list[str] = []
    while index < len(tag):
        char = tag[index]
        index += 1
        if quote is not None:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            chars.append(char)
            continue
        if char == "{":
            depth += 1
            chars.append(char)
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return "".join(chars).strip()
            chars.append(char)
            continue
        chars.append(char)
    return None


def _source_refs(
    frontmatter: dict[str, Any],
    body: str,
    quiz_items: list[dict[str, Any]],
    card_items: list[dict[str, Any]],
) -> list[str]:
    refs: list[str] = []
    _add_refs(refs, _as_str_list(frontmatter.get("source_refs")))
    _add_refs(refs, re.findall(r"<!--\s*source_ref:\s*([A-Za-z0-9_.:/-]+)\s*-->", body))
    _add_refs(refs, _source_ref_ids(body))
    _add_refs(refs, re.findall(r"^\s*-\s*`([^`]+)`\s*:", body, flags=re.MULTILINE))
    for item in [*quiz_items, *card_items]:
        _add_refs(refs, _item_source_refs(item))
    return refs


def source_refs_from_text(text: str) -> list[str]:
    refs: list[str] = []
    _add_refs(refs, re.findall(r"<!--\s*source_ref:\s*([A-Za-z0-9_.:/-]+)\s*-->", text))
    _add_refs(refs, _source_ref_ids(text))
    _add_refs(refs, re.findall(r"^\s*-\s*`([^`]+)`\s*:", text, flags=re.MULTILINE))
    return refs



def _source_ref_ids(text: str) -> list[str]:
    ids: list[str] = []
    for match in re.finditer(rf"<SourceRef\b(?P<attrs>{_TAG_ATTRS})>", text, flags=re.DOTALL):
        attrs = match.group("attrs") or ""
        value = _prop_value(attrs, "id")
        if value is None:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        ids.append(value)
    return ids

def _item_source_refs(item: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    _add_refs(refs, _as_str_list(item.get("source_refs")))
    citations = item.get("citations")
    if isinstance(citations, list):
        for citation in citations:
            if isinstance(citation, dict):
                ref_id = citation.get("ref_id")
                if isinstance(ref_id, str) and ref_id.strip():
                    refs.append(ref_id.strip())
    return refs


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _add_refs(refs: list[str], values: list[str]) -> None:
    seen = set(refs)
    for value in values:
        clean = value.strip()
        if clean and clean not in seen:
            refs.append(clean)
            seen.add(clean)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
