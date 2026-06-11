from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Coarse offline per-node estimates (CNY) for ``--dry-run`` only; the real per-call
# cost is computed by the runtime from registered prices (see scheduler.llm). Values
# are rough order-of-magnitude figures in RMB to match the providers' billing currency.
ESTIMATE: dict[str, dict[str, float]] = {
    "source_summary": {"tokens": 400, "cost_cny": 0.0014},
    "caption": {"tokens": 300, "cost_cny": 0.0014},
    "structure": {"tokens": 900, "cost_cny": 0.0070},
    "split": {"tokens": 300, "cost_cny": 0.0007},
    "chapter": {"tokens": 1200, "cost_cny": 0.0140},
    "summary": {"tokens": 300, "cost_cny": 0.0014},
    "quiz": {"tokens": 700, "cost_cny": 0.0056},
    "card": {"tokens": 250, "cost_cny": 0.0007},
    "concept": {"tokens": 500, "cost_cny": 0.0028},
    "review": {"tokens": 800, "cost_cny": 0.0070},
}


@dataclass(frozen=True)
class Estimate:
    tokens: int
    cost_cny: float


def estimate(agent_cls: type[Any] | str, *inputs: Any) -> Estimate:
    kind = agent_cls if isinstance(agent_cls, str) else getattr(agent_cls, "kind", "chapter")
    base = ESTIMATE.get(kind, {"tokens": 100, "cost_cny": 0.0007})
    size = sum(len(str(item)) for item in inputs)
    tokens = int(base["tokens"] + size / 4)
    return Estimate(
        tokens=tokens, cost_cny=round(float(base["cost_cny"]) * max(1, tokens / 500), 6)
    )


def summarize(nodes: list[str], chapter_count: int = 2) -> Estimate:
    tokens = 0
    cost = 0.0
    for node in nodes:
        multiplier = chapter_count if node in {"generate", "concept_pages"} else 1
        item = estimate(node)
        tokens += item.tokens * multiplier
        cost += item.cost_cny * multiplier
    return Estimate(tokens=tokens, cost_cny=round(cost, 6))
