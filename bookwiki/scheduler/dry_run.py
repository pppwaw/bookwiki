from __future__ import annotations

from dataclasses import dataclass

# Per-graph-node cost model for ``--dry-run`` only; the real per-call cost is computed
# by the runtime from registered prices (see scheduler.llm). Figures are calibrated
# from the real ``llm_usage`` of a full ``calculus`` run — 40 sections, 120 concepts
# (see ``books/calculus/work/logs/run-manifest.json``).
#
# Keys are the graph nodes in ``scheduler.resume.NODE_ORDER``. Each node is either a
# one-shot cost or scales with the book's section count (``per_chapter``).
#
# Source of each figure (a single clean generation, NOT the sum of every re-run):
# * The compute-heavy nodes (``generate`` / ``concept_pages`` / ``build_skeleton`` /
#   ``split`` / ``repair``) come from the one run that re-ran everything after
#   ``convert`` — ¥67.80 total — divided by the 40 sections, so
#   ``summarize(NODE_ORDER, chapter_count=40)`` reproduces that clean pass.
# * ``caption`` / ``structure`` were cache-hits (¥0) in that re-run, so they use their
#   first real execution instead — a fresh, uncached book must pay for them.
# * ``index`` uses the most recent index pass.
# Caveats: ``caption`` actually scales with the figure count and ``index`` with the
# corpus size, but those counts are unknown at dry-run time, so both are modelled as
# one-shot at calculus's values — a figure-heavy book is under-estimated there.
# ``convert`` is MinerU (no LLM); ``reconcile_concepts`` / ``integrate`` / ``check``
# spend nothing on their own.


@dataclass(frozen=True)
class NodeCost:
    tokens: int
    cost_cny: float
    per_chapter: bool = False


ESTIMATE: dict[str, NodeCost] = {
    "convert": NodeCost(0, 0.0),
    "caption": NodeCost(1_247_894, 2.010287),
    "structure": NodeCost(909_093, 1.026168),
    "split": NodeCost(42_234, 0.044038),
    "build_skeleton": NodeCost(29_299, 0.030981, per_chapter=True),
    "generate": NodeCost(371_744, 0.898002, per_chapter=True),
    "reconcile_concepts": NodeCost(0, 0.0),
    "concept_pages": NodeCost(248_425, 0.763964, per_chapter=True),
    "integrate": NodeCost(0, 0.0),
    "check": NodeCost(0, 0.0),
    "repair": NodeCost(359, 0.000871, per_chapter=True),
    "index": NodeCost(806_592, 0.054848),
}

# Unknown nodes fall back to a negligible non-zero cost so a graph change never makes
# the estimate silently read as free.
_FALLBACK = NodeCost(100, 0.0007)


@dataclass(frozen=True)
class Estimate:
    tokens: int
    cost_cny: float


def summarize(nodes: list[str], chapter_count: int = 2) -> Estimate:
    tokens = 0
    cost = 0.0
    for node in nodes:
        cost_model = ESTIMATE.get(node, _FALLBACK)
        multiplier = max(1, chapter_count) if cost_model.per_chapter else 1
        tokens += cost_model.tokens * multiplier
        cost += cost_model.cost_cny * multiplier
    return Estimate(tokens=tokens, cost_cny=round(cost, 6))
