from __future__ import annotations

from bookwiki.schemas.skeleton import SkeletonOp, SplitTarget
from bookwiki.skeleton.fold import Registry


def _add(canonical: str, *aliases: str) -> SkeletonOp:
    return SkeletonOp(op="add_concept", canonical=canonical, aliases=list(aliases))


def test_add_concept_sets_first_chapter_to_current() -> None:
    reg = Registry()
    reg.apply([_add("反向传播")], current_chapter="ch03")
    skeleton = reg.to_skeleton(chapter_briefs={}, chapter_order=["ch01", "ch03"])
    assert len(skeleton.glossary) == 1
    assert skeleton.glossary[0].canonical == "反向传播"
    assert skeleton.glossary[0].first_chapter_id == "ch03"


def test_add_concept_idempotent_keeps_first_owner() -> None:
    reg = Registry()
    reg.apply([_add("梯度下降")], current_chapter="ch02")
    # A later chapter re-adds the same concept: ownership must NOT move.
    reg.apply([_add("梯度下降", "gradient descent")], current_chapter="ch05")
    skeleton = reg.to_skeleton(chapter_briefs={}, chapter_order=["ch02", "ch05"])
    assert len(skeleton.glossary) == 1
    entry = skeleton.glossary[0]
    assert entry.first_chapter_id == "ch02"
    assert "gradient descent" in entry.aliases


def test_add_alias_attaches_and_resolves() -> None:
    reg = Registry()
    reg.apply([_add("反向传播")], current_chapter="ch01")
    reg.apply(
        [SkeletonOp(op="add_alias", canonical="反向传播", alias="BP")],
        current_chapter="ch02",
    )
    skeleton = reg.to_skeleton(chapter_briefs={}, chapter_order=["ch01", "ch02"])
    assert skeleton.glossary[0].aliases == ["BP"]
    # alias_map maps both raw + normalised variants to canonical.
    assert skeleton.alias_map["BP"] == "反向传播"
    assert skeleton.alias_map["bp"] == "反向传播"


def test_rename_canonical_promotes_name_and_demotes_old() -> None:
    reg = Registry()
    reg.apply([_add("反传")], current_chapter="ch01")
    reg.apply(
        [SkeletonOp(op="rename_canonical", from_canonical="反传", to_canonical="反向传播")],
        current_chapter="ch01",
    )
    skeleton = reg.to_skeleton(chapter_briefs={}, chapter_order=["ch01"])
    entry = skeleton.glossary[0]
    assert entry.canonical == "反向传播"
    assert "反传" in entry.aliases
    assert entry.first_chapter_id == "ch01"  # ownership preserved across rename


def test_merge_folds_loser_into_winner_with_earliest_owner() -> None:
    reg = Registry()
    # Cross-language synonyms introduced in different chapters.
    reg.apply([_add("反向传播")], current_chapter="ch03")
    reg.apply([_add("backpropagation")], current_chapter="ch07")
    reg.apply(
        [SkeletonOp(op="merge", winner="backpropagation", loser="反向传播")],
        current_chapter="ch07",
    )
    skeleton = reg.to_skeleton(chapter_briefs={}, chapter_order=["ch03", "ch07"])
    assert len(skeleton.glossary) == 1
    entry = skeleton.glossary[0]
    assert entry.canonical == "backpropagation"
    assert "反向传播" in entry.aliases
    # Earliest introducing chapter wins ownership even though the winner came later.
    assert entry.first_chapter_id == "ch03"
    assert skeleton.alias_map["反向传播"] == "backpropagation"


def test_merge_then_split_round_trips_to_two_concepts() -> None:
    reg = Registry()
    reg.apply([_add("A")], current_chapter="ch01")
    reg.apply([_add("B")], current_chapter="ch02")
    reg.apply([SkeletonOp(op="merge", winner="A", loser="B")], current_chapter="ch02")
    assert len(reg.to_skeleton(chapter_briefs={}, chapter_order=[]).glossary) == 1
    # An early wrong merge can be undone by a later split.
    reg.apply(
        [
            SkeletonOp(
                op="split",
                canonical="A",
                into=[SplitTarget(canonical="A"), SplitTarget(canonical="B")],
            )
        ],
        current_chapter="ch05",
    )
    skeleton = reg.to_skeleton(chapter_briefs={}, chapter_order=["ch01", "ch02"])
    canonicals = sorted(c.canonical for c in skeleton.glossary)
    assert canonicals == ["A", "B"]
    assert skeleton.alias_map["A"] == "A"
    assert skeleton.alias_map["B"] == "B"
    owners = {entry.canonical: entry.first_chapter_id for entry in skeleton.glossary}
    assert owners == {"A": "ch01", "B": "ch02"}


def test_unknown_op_targets_are_ignored_not_crashed() -> None:
    reg = Registry()
    reg.apply(
        [
            SkeletonOp(op="add_alias", canonical="does-not-exist", alias="x"),
            SkeletonOp(op="merge", winner="nope", loser="nada"),
            SkeletonOp(op="rename_canonical", from_canonical="ghost", to_canonical="y"),
            SkeletonOp(op="split", canonical="absent", into=[SplitTarget(canonical="z")]),
        ],
        current_chapter="ch01",
    )
    assert reg.to_skeleton(chapter_briefs={}, chapter_order=[]).glossary == []


def test_record_uses_resolves_aliases_to_canonical() -> None:
    reg = Registry()
    reg.apply([_add("反向传播", "BP")], current_chapter="ch01")
    reg.record_uses("ch04", ["bp", "unknown-term"])
    # Alias resolves to canonical; unknown term is dropped.
    assert reg.uses_for("ch04") == ["反向传播"]
