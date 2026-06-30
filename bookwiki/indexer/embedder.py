from __future__ import annotations

import math
import struct
from typing import Any

from bookwiki.scheduler.llm import OPENROUTER_USD_TO_CNY

DEFAULT_EMBED_MODEL = "baai/bge-m3"
DEFAULT_EMBED_DIM = 1024

# OpenRouter listed price for baai/bge-m3 at integration time: $0.01 / 1M input
# tokens (embeddings have no output tokens). Converted to CNY with the same fixed
# guardrail rate the LLM Router uses, so manual accounting matches the rest of the
# run's budget currency. Update here if the model or its price changes.
EMBED_PRICE_USD_PER_1M = 0.01


def floats_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def embed_cost_cny(prompt_tokens: int, *, price_usd_per_1m: float = EMBED_PRICE_USD_PER_1M) -> float:
    return prompt_tokens / 1_000_000 * price_usd_per_1m * OPENROUTER_USD_TO_CNY


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _usage_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None and isinstance(usage, dict):
        prompt = usage.get("prompt_tokens") or usage.get("total_tokens")
    if prompt is None:
        prompt = getattr(usage, "total_tokens", 0)
    return int(prompt or 0)


def _raw_embed(
    texts: list[str], *, model: str, api_key: str, base_url: str
) -> tuple[list[list[float]], int]:
    # Lazy import keeps the module (and the test suite, which stubs ``_raw_embed``)
    # free of litellm's heavy import. ``custom_llm_provider='openai'`` routes the
    # OpenAI-compatible /embeddings call to the given OpenRouter base verbatim.
    from litellm import embedding

    resp = embedding(
        model=model,
        input=texts,
        api_key=api_key,
        api_base=base_url,
        custom_llm_provider="openai",
    )
    embeddings = [item["embedding"] for item in resp["data"]]
    tokens = _usage_tokens(resp.get("usage") if hasattr(resp, "get") else getattr(resp, "usage", None))
    return embeddings, tokens


def embed_texts(
    texts: list[str], *, model: str, api_key: str, base_url: str
) -> tuple[list[list[float]], int]:
    if not texts:
        return [], 0
    raw, tokens = _raw_embed(texts, model=model, api_key=api_key, base_url=base_url)
    return [_normalize(vec) for vec in raw], tokens
