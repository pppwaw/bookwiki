from __future__ import annotations

from bookwiki.schemas.common import VersionedModel


class ImageSupplementResult(VersionedModel):
    """Outcome of supplementing one section figure (Phase 4).

    Produced by ``SupplementImageAgent`` for a single ``FigureRequest``. When
    ``ok`` is true, ``book_figure_tag`` is the canonical ``<BookFigure/>`` tag
    (with ``src``/``caption``) that the integrator should resolve the section's
    placeholder reference to; ``image_path`` is the generated asset relative to
    ``book_dir``. Failures are best-effort: ``ok=False`` with an ``error`` never
    aborts the chapter, the unresolved placeholder is simply dropped at render.
    """

    chapter_id: str
    section_index: int
    figure_ref: str
    ok: bool
    image_path: str = ""
    caption: str = ""
    book_figure_tag: str = ""
    error: str = ""
    owner_task_id: str
