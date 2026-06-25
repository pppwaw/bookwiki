from __future__ import annotations

import re


def concept_key(value: str) -> str:
    """Normalised identity key for a concept name; shared by skeleton fold,
    reconcile, and integrator. Keep exactly in sync with the original
    ``_concept_key`` implementations.
    """
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def brief_for(title: str, topics: list[str]) -> str:
    """Shared chapter-brief builder for skeleton drafts and streaming folds."""
    head = title or (topics[0] if topics else "")
    if topics:
        joined = "、".join(topics[:3])
        sentence = f"{head}：{joined}" if head and head not in joined else joined
    else:
        sentence = head
    sentence = sentence.strip()
    return sentence[:80]
