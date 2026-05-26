from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import (
    chapter_document,
    chapter_id,
    chapter_title,
    citation,
    source_refs,
)
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.quiz import QuizItem, QuizPlacement, QuizResult


class QuizAgent:
    kind: ClassVar[str] = "quiz_llm_v1"
    output_model: ClassVar[type[QuizResult]] = QuizResult
    model_key: ClassVar[str] = "quiz"
    prompt_name: ClassVar[str] = "quiz"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""You are the quiz-generation agent. Design questions a great tutor
would use to surface real understanding, not pattern-matching.

What good questions look like:
- Each question probes a single specific idea from the chapter (a definition, a
  step in a derivation, a property, an interpretation, a common confusion).
- Stems are concrete and learner-friendly. When useful, set a tiny scenario
  ("Suppose you observe...", "Given the estimator above...").
- Distractors must be plausible: a tempting wrong choice should reflect a real
  misconception a learner could hold after a fast read of the chapter.
- Avoid trivia ("how many sections..."), trick wording, and answers that need
  knowledge from outside the chapter.

Explanations:
- After "the answer is X" briefly say *why* in one or two sentences, and
  explicitly name the misconception that would lead to the most common wrong
  choice. Tie back to a specific chapter idea.

Constraints:
- Create multiple-choice questions that test understanding, not trivia.
- Create an appropriate number of questions, using quiz_per_chapter as an upper
  bound or target when provided.
- Each question must have at least two plausible choices and exactly one answer
  matching one of the choices.
- Explanations should teach why the answer is correct.
- Use citations from the chapter source for each item.

Math:
- Use Markdown math syntax in questions, choices, answers, and explanations:
  $...$ for inline formulas and $$...$$ for display formulas.
- Do not use \\( ... \\) or \\[ ... \\] math delimiters.

Placement (CRITICAL — interleave, do not front-load):
- Do NOT put every item in a single placement at the start of the chapter.
- Read `chapter_body_blocks` (0-indexed list of paragraphs). Create one
  placement per logical section, so a learner answers each question right
  after reading the material it tests — like a mid-article checkpoint.
- Aim for 2-4 placements per chapter, each holding 1-2 items. A placement
  with more than 2 items is acceptable only when the section is unusually
  long or those items truly belong together.
- `placements.after_block` is the 0-based index into chapter_body_blocks
  AFTER which the QuizBlock is inserted. Pick the block index that ends
  the section the placement covers. Spread these indexes across the
  chapter — do not bunch them near 0.
- `placements.item_indexes` is 1-based into `items`. Every item must
  appear in exactly one placement; no item may appear twice.
- `placements.title` is a short heading like "Checkpoint", "Quick check",
  "Practice", or a section-specific phrase. Avoid the generic "Quiz" when
  a more descriptive title fits.

Avoid trick questions, ambiguous wording, and answers that require outside knowledge.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> QuizResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        refs = source_refs(inp)
        count = _requested_count(inp, "quiz_per_chapter", "quizPerChapter", 1)
        items = [
            QuizItem(
                question=f"What is central idea {index + 1} in {title}?",
                choices=[title, "Unrelated topic"],
                answer=title,
                explanation="The answer should be grounded in the chapter source.",
                citations=[citation(inp)],
            )
            for index in range(count)
        ]
        draft = QuizResult(
            chapter_id=ch_id,
            items=items,
            placements=[
                QuizPlacement(
                    after_block=0,
                    item_indexes=list(range(1, len(items) + 1)),
                    title="Quiz",
                )
            ],
            owner_task_id=f"{ch_id}:quiz",
        )
        llm_input = _content_input(inp, refs)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=QuizResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=llm_input,
            draft=draft,
            allowed_citation_refs=refs,
        )
        return QuizResult.model_validate(result)


def _content_input(inp: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    payload = {key: value for key, value in inp.items() if key != "source_md"}
    payload["document_xml"] = chapter_document(inp)
    payload["allowed_source_refs"] = sorted(refs)
    body = str(inp.get("chapter_body_md", ""))
    if body:
        payload["chapter_body_blocks"] = [
            block.strip() for block in body.split("\n\n") if block.strip()
        ]
    return payload


def _requested_count(inp: dict[str, Any], snake_key: str, camel_key: str, default: int) -> int:
    try:
        value = int(inp.get(snake_key, inp.get(camel_key, default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
