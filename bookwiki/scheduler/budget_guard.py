from __future__ import annotations

from typing import Any


class BudgetExceeded(RuntimeError):
    """Raised when accumulated router cost exceeds the configured budget."""


def total_cost_usd(usage_logs: Any) -> float:
    if not usage_logs:
        return 0.0
    if isinstance(usage_logs, dict):
        return float(usage_logs.get("cost") or usage_logs.get("cost_usd") or 0.0)
    total = 0.0
    for item in usage_logs:
        if isinstance(item, dict):
            total += float(item.get("cost") or item.get("cost_usd") or 0.0)
    return total


def enforce_budget(router: Any, max_cost_usd: float) -> None:
    spent = total_cost_usd(getattr(router, "usage_logs", None))
    if spent > max_cost_usd:
        raise BudgetExceeded(f"budget exceeded: spent ${spent:.4f}, limit ${max_cost_usd:.4f}")
