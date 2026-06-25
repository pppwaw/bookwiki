from __future__ import annotations

import pytest

from bookwiki.chunking import Chunk, chunk_budget, chunk_by_heading
from bookwiki.scheduler.llm import count_text_tokens, input_token_budget

MODEL = "deepseek-v4-flash"


def _tokens(text: str) -> int:
    return count_text_tokens(text, model=MODEL)


def test_chunk_budget_stays_below_compact_input_cap() -> None:
    cap = input_token_budget(MODEL)  # 662_528
    structure = chunk_budget(MODEL, stage="structure")
    skeleton = chunk_budget(MODEL, stage="skeleton")
    # The whole reason this exists: a chunk must never reach the per-field truncation cap.
    assert structure < cap
    assert skeleton < cap
    # structure (0.7) leaves more room than skeleton (0.6); both are sizeable.
    assert structure > skeleton > 100_000


def test_chunk_budget_rejects_unknown_stage() -> None:
    with pytest.raises(ValueError, match="unknown chunking stage"):
        chunk_budget(MODEL, stage="nonsense")


def test_small_text_returns_single_chunk_with_full_text() -> None:
    text = "# 第1章 引言\n\n这是一段很短的正文。\n"
    chunks = chunk_by_heading(text, model=MODEL, stage="structure")
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)


def test_every_chunk_stays_within_budget() -> None:
    body = "\n\n".join(f"第 {i} 段，反向传播与梯度下降的推导过程。" for i in range(40))
    text = f"# 第1章 神经网络\n\n{body}\n"
    budget = max(20, _tokens(text) // 4)
    chunks = chunk_by_heading(text, model=MODEL, stage="structure", budget=budget)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _tokens(chunk.text) <= budget


def test_recursive_descent_splits_at_deeper_heading_when_h1_block_too_big() -> None:
    sub = "\n\n".join("反向传播链式法则的逐层推导细节。" for _ in range(12))
    text = (
        "# 第3章 神经网络\n\n章首导言。\n\n"
        f"## 3.1 前向传播\n\n{sub}\n\n"
        f"## 3.2 反向传播\n\n{sub}\n"
    )
    budget = max(20, _tokens(text) // 4)
    chunks = chunk_by_heading(text, model=MODEL, stage="structure", budget=budget)
    # At least one chunk must carry the deeper (H2) heading in its path — proving the
    # over-budget H1 block was re-split at the next level down.
    h2_paths = [c.heading_path for c in chunks if len(c.heading_path) >= 2]
    assert h2_paths, f"expected H2-level chunks, got paths: {[c.heading_path for c in chunks]}"
    assert any("3.2 反向传播" in path[-1] for path in h2_paths)
    # Every chunk under chapter 3 keeps the H1 as its first path element.
    for chunk in chunks:
        if chunk.heading_path:
            assert chunk.heading_path[0] == "第3章 神经网络"


def test_char_fallback_overlaps_when_no_finer_heading() -> None:
    # One heading, one giant paragraph with no sub-headings -> char fallback.
    para = "梯度下降按负梯度方向迭代更新参数直到收敛。" * 60
    text = f"# 第2章 优化\n\n{para}\n"
    budget = max(20, _tokens(text) // 5)
    chunks = chunk_by_heading(text, model=MODEL, stage="structure", budget=budget)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _tokens(chunk.text) <= budget
    # Adjacent fallback windows overlap: the next chunk starts before the previous ends.
    assert any(
        nxt.char_start < cur.char_end for cur, nxt in zip(chunks, chunks[1:], strict=False)
    )


def test_source_refs_union_equals_full_set() -> None:
    parts = []
    for i in range(30):
        parts.append(f"## 小节 {i}\n\n<!-- source_ref:ref-{i:03d} -->\n正文内容若干。")
    text = "# 第4章 覆盖\n\n" + "\n\n".join(parts) + "\n"
    expected = {f"ref-{i:03d}" for i in range(30)}
    budget = max(20, _tokens(text) // 6)
    chunks = chunk_by_heading(text, model=MODEL, stage="structure", budget=budget)
    union = {ref for chunk in chunks for ref in chunk.source_refs}
    assert union == expected, f"missing refs: {expected - union}"


def test_char_offsets_index_into_original_text() -> None:
    body = "\n\n".join(f"第 {i} 段正文。" for i in range(50))
    text = f"# 第5章 偏移\n\n{body}\n"
    budget = max(20, _tokens(text) // 4)
    chunks = chunk_by_heading(text, model=MODEL, stage="structure", budget=budget)
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_headings_inside_code_fence_are_not_split_points() -> None:
    text = (
        "# 第6章 代码\n\n正文。\n\n"
        "```python\n# 这不是标题\n## 也不是\nprint('x')\n```\n\n"
        "## 6.1 真小节\n\n收尾正文。\n"
    )
    chunks = chunk_by_heading(text, model=MODEL, stage="structure")
    assert len(chunks) == 1  # fits in one chunk; fence headings must not have mattered
    assert isinstance(chunks[0], Chunk)
