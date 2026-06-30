from __future__ import annotations

import json
import math
import struct
import urllib.error
import urllib.request

from bookwiki.scheduler.llm import OPENROUTER_USD_TO_CNY

DEFAULT_EMBED_MODEL = "baai/bge-m3"
DEFAULT_EMBED_DIM = 1024

# OpenRouter listed price for baai/bge-m3 at integration time: $0.01 / 1M input
# tokens (embeddings have no output tokens). Converted to CNY with the same fixed
# guardrail rate the LLM Router uses, so manual accounting matches the rest of the
# run's budget currency. Update here if the model or its price changes.
EMBED_PRICE_USD_PER_1M = 0.01

_EMBED_TIMEOUT_SECONDS = 120
_EMBED_BATCH = 64


def floats_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def embed_cost_cny(prompt_tokens: int, *, price_usd_per_1m: float = EMBED_PRICE_USD_PER_1M) -> float:
    return prompt_tokens / 1_000_000 * price_usd_per_1m * OPENROUTER_USD_TO_CNY


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _raw_embed(
    texts: list[str], *, model: str, api_key: str, base_url: str
) -> tuple[list[list[float]], int]:
    # Direct call to the OpenAI-compatible /embeddings endpoint. litellm's embedding
    # response converter raises an opaque empty-message error against OpenRouter, so
    # we own the request/parse here and surface the real response body on failure.
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"embedding 请求失败 {exc.code} ({model}): {detail}") from exc

    data = body.get("data")
    if not data:
        raise RuntimeError(f"embedding 响应缺少 data ({model}): {json.dumps(body)[:500]}")
    embeddings = [item["embedding"] for item in data]
    usage = body.get("usage") or {}
    tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
    return embeddings, tokens


def embed_texts(
    texts: list[str], *, model: str, api_key: str, base_url: str
) -> tuple[list[list[float]], int]:
    if not texts:
        return [], 0
    vectors: list[list[float]] = []
    total_tokens = 0
    for start in range(0, len(texts), _EMBED_BATCH):
        batch = texts[start : start + _EMBED_BATCH]
        raw, tokens = _raw_embed(batch, model=model, api_key=api_key, base_url=base_url)
        vectors.extend(_normalize(vec) for vec in raw)
        total_tokens += tokens
    return vectors, total_tokens
