from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bookwiki.utils.files import ensure_dir, write_json

DEFAULT_MODELS = {
    "source_summary": "deepseek-v4-flash",
    "source_layout_repair": "deepseek-v4-flash",
    "structure": "deepseek-v4-pro",
    "split": "deepseek-v4-flash",
    "skeleton": "deepseek-v4-flash",
    "section_planner": "deepseek-v4-pro",
    "section": "deepseek-v4-pro",
    "section_repair": "deepseek-v4-pro",
    "application_quiz": "deepseek-v4-pro",
    "application_quiz_worked": "deepseek-v4-pro",
    "card": "deepseek-v4-flash",
    "supplement_image": "deepseek-v4-pro",
    "mdx_repair": "deepseek-v4-pro",
    "quality_check": "deepseek-v4-flash",
    "quality_rewrite": "deepseek-v4-pro",
    "summary": "deepseek-v4-flash",
    "concept": "deepseek-v4-pro",
    "review": "deepseek-v4-pro",
    "vision": "openrouter-qwen3.6-35b-a3b",
}

DEFAULT_BUDGET = {"maxCostCny": 70.0}


DEFAULT_GENERATION = {
    "quizPerChapter": 5,
    "cardsPerChapter": 8,
    "maxChapterConcurrency": 4,
    "maxSectionConcurrency": 3,
    "maxRepairRounds": 3,
    "qualityCheck": False,
    "maxQualityRounds": 2,
    "allowMissingMdxValidator": False,
    "siteTypeCheck": "auto",
    "sourceLayoutRepair": {
        "mode": "auto",
        "minConfidence": 0.85,
        "maxCandidatesPerSource": 20,
    },
    "visionCaption": {
        "mode": "auto",
        # A whole book is often a single source; 20 captions cannot cover a 1000-page
        # book's figures, so 90% of images would go uncaptioned. Scale for large books.
        "maxImagesPerSource": 200,
        "maxConcurrent": 10,
    },
}


@dataclass
class BookConfig:
    book_dir: Path
    book_id: str
    title: str
    language: str = "zh-CN"
    notes_path: str = "book.notes.md"
    models: dict[str, str] = field(default_factory=lambda: DEFAULT_MODELS.copy())
    budget: dict[str, Any] = field(default_factory=lambda: DEFAULT_BUDGET.copy())
    generation: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_GENERATION))
    pause_after: list[str] = field(default_factory=list)
    dry_run: bool = False
    force_from: str | None = None
    force_clear_cache: bool = False
    target_chapters: list[str] = field(default_factory=list)
    target_concepts: list[str] = field(default_factory=list)
    llm_runtime: Any | None = None

    @property
    def input_dir(self) -> Path:
        return self.book_dir / "input"

    @property
    def work_dir(self) -> Path:
        return self.book_dir / "work"

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / ".cache"

    @property
    def content_dir(self) -> Path:
        # Single source of truth: integrate renders straight into the site, and check/repair/index
        # operate on it in place. The whole pipeline follows this one property into ``site``.
        return self.site_dir / "content" / "docs"

    @property
    def site_dir(self) -> Path:
        return self.book_dir / "site"

    @property
    def notes_file(self) -> Path:
        return self.book_dir / self.notes_path

    @property
    def book_notes(self) -> str:
        if not self.notes_file.exists():
            return ""
        return self.notes_file.read_text(encoding="utf-8").strip()

    def model_for(self, key: str) -> str:
        return self.models.get(key, "stub")

    @property
    def target_chapter_ids(self) -> set[str]:
        return {str(ch_id).strip() for ch_id in self.target_chapters if str(ch_id).strip()}

    @property
    def target_concept_names(self) -> set[str]:
        return {str(name).strip() for name in self.target_concepts if str(name).strip()}

    @property
    def quiz_per_chapter(self) -> int:
        return _positive_int(
            self.generation.get("quizPerChapter"), DEFAULT_GENERATION["quizPerChapter"]
        )

    @property
    def cards_per_chapter(self) -> int:
        return _positive_int(
            self.generation.get("cardsPerChapter"), DEFAULT_GENERATION["cardsPerChapter"]
        )

    @property
    def chapter_concurrency(self) -> int:
        return _positive_int(
            self.generation.get("maxChapterConcurrency"),
            DEFAULT_GENERATION["maxChapterConcurrency"],
        )

    @property
    def section_concurrency(self) -> int:
        return _positive_int(
            self.generation.get("maxSectionConcurrency"),
            DEFAULT_GENERATION["maxSectionConcurrency"],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "title": self.title,
            "language": self.language,
            "notesPath": self.notes_path,
            "models": self.models,
            "budget": self.budget,
            "generation": self.generation,
        }


def default_config(book_dir: str | Path, title: str | None = None) -> BookConfig:
    path = Path(book_dir)
    return BookConfig(
        book_dir=path, book_id=path.name, title=title or path.name.replace("-", " ").title()
    )


def load_config(book_dir: str | Path) -> BookConfig:
    path = Path(book_dir)
    config_path = path / "book.config.json"
    if not config_path.exists():
        cfg = default_config(path)
        save_config(cfg)
        return cfg

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    budget = DEFAULT_BUDGET.copy()
    raw_budget = raw.get("budget", {})
    if isinstance(raw_budget, dict) and "maxCostCny" in raw_budget:
        budget["maxCostCny"] = raw_budget["maxCostCny"]
    return BookConfig(
        book_dir=path,
        book_id=str(raw.get("book_id") or path.name),
        title=str(raw.get("title") or path.name),
        language=str(raw.get("language") or "zh-CN"),
        notes_path=str(raw.get("notesPath") or raw.get("notes_path") or "book.notes.md"),
        models={**DEFAULT_MODELS, **raw.get("models", {})},
        budget=budget,
        generation=_merge_generation(raw.get("generation", {})),
    )


def save_config(cfg: BookConfig) -> Path:
    ensure_dir(cfg.book_dir)
    return write_json(cfg.book_dir / "book.config.json", cfg.to_json())


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _merge_generation(raw: Any) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_GENERATION)
    if not isinstance(raw, dict):
        return merged
    for key, value in raw.items():
        if key in {"sourceLayoutRepair", "visionCaption"} and isinstance(value, dict):
            nested = merged.get("sourceLayoutRepair")
            if key == "visionCaption":
                nested = merged.get("visionCaption")
            if isinstance(nested, dict):
                nested.update(value)
            else:
                merged[key] = value
        else:
            merged[key] = value
    return merged
