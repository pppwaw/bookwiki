from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ESTIMATE: dict[str, dict[str, float]] = {
    "source_summary": {"tokens": 400, "cost_usd": 0.0002},
    "caption": {"tokens": 300, "cost_usd": 0.0002},
    "structure": {"tokens": 900, "cost_usd": 0.0010},
    "split": {"tokens": 300, "cost_usd": 0.0001},
    "chapter": {"tokens": 1200, "cost_usd": 0.0020},
    "summary": {"tokens": 300, "cost_usd": 0.0002},
    "quiz": {"tokens": 700, "cost_usd": 0.0008},
    "card": {"tokens": 250, "cost_usd": 0.0001},
    "concept": {"tokens": 500, "cost_usd": 0.0004},
    "review": {"tokens": 800, "cost_usd": 0.0010},
}


@dataclass(frozen=True)
class Estimate:
    tokens: int
    cost_usd: float


def estimate(agent_cls: type[Any] | str, *inputs: Any) -> Estimate:
    kind = agent_cls if isinstance(agent_cls, str) else getattr(agent_cls, "kind", "chapter")
    base = ESTIMATE.get(kind, {"tokens": 100, "cost_usd": 0.0001})
    size = sum(len(str(item)) for item in inputs)
    tokens = int(base["tokens"] + size / 4)
    return Estimate(
        tokens=tokens, cost_usd=round(float(base["cost_usd"]) * max(1, tokens / 500), 6)
    )


def summarize(nodes: list[str], chapter_count: int = 2) -> Estimate:
    tokens = 0
    cost = 0.0
    for node in nodes:
        multiplier = chapter_count if node in {"generate", "concept_pages"} else 1
        item = estimate(node)
        tokens += item.tokens * multiplier
        cost += item.cost_usd * multiplier
    return Estimate(tokens=tokens, cost_usd=round(cost, 6))
