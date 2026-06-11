from __future__ import annotations


class BudgetExceeded(RuntimeError):
    """Raised when accumulated LLM cost exceeds the configured ``maxCostUsd``.

    Enforcement lives in ``LiteLLMRuntime._record_usage`` (see
    ``bookwiki.scheduler.llm``): every API response accumulates token/cost usage and
    raises this once the running total crosses the budget. The runtime is the single
    source of truth for spend, so the old router-``usage_logs`` polling helpers were
    removed in favour of in-runtime accounting.
    """
