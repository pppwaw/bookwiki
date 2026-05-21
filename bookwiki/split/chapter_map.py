from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChapterMapItem:
    chapter_id: str
    title: str
    source_path: str
