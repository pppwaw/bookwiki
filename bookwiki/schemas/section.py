from __future__ import annotations

from pydantic import Field

from bookwiki.schemas.common import Citation, VersionedModel


class SectionSpec(VersionedModel):
    """One teaching unit within a chapter, planned by ``SectionPlannerAgent``.

    A section groups one or more chapter ``topics`` into a coherent unit. The
    planner may bind several related topics to a single section or add a short
    bridging section, but the number of sections never exceeds the number of
    topics (with a floor of one section so topic-less chapters still produce
    output).
    """

    chapter_id: str
    index: int = Field(ge=0)
    title: str
    topics_covered: list[str] = Field(default_factory=list)
    concepts_introduced: list[str] = Field(default_factory=list)
    learning_goal: str


class SectionPlan(VersionedModel):
    """The ordered list of teaching units for a single chapter."""

    chapter_id: str
    sections: list[SectionSpec] = Field(default_factory=list)
    owner_task_id: str


class FigureRequest(VersionedModel):
    """A section's declared need for a figure (consumed in Phase 4).

    ``kind`` is one of ``none`` / ``reuse_existing`` / ``plot``. In Phase 3 the
    section agent leaves this empty; the field exists so the schema stays stable
    when the supplement-image tooling lands.
    """

    kind: str = "none"
    figure_ref: str = ""
    rationale: str = ""


class SectionResult(VersionedModel):
    """A single generated section body fragment.

    Sections only carry prose (plus the concepts and citations they introduce);
    quiz items and recall cards are produced once at chapter level by
    ``QuizCardAgent`` after the sections are assembled into the full chapter
    body. ``body_md`` must NOT include the chapter ``# H1`` heading; the
    assembler adds it once and prefixes each section with its own ``##`` title.
    """

    chapter_id: str
    section_index: int = Field(ge=0)
    title: str
    body_md: str
    concepts: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    figure_requests: list[FigureRequest] = Field(default_factory=list)
    owner_task_id: str
