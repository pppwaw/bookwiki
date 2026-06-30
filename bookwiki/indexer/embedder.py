from __future__ import annotations

import json
import math
import struct
import urllib.request

DEFAULT_EMBED_MODEL = "baai/bge-m3"
DEFAULT_EMBED_DIM = 1024


def floats_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _raw_embed(
    texts: list[str], *, model: str, api_key: str, base_url: str
) -> list[list[float]]:
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
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [item["embedding"] for item in body["data"]]


def embed_texts(
    texts: list[str], *, model: str, api_key: str, base_url: str
) -> list[list[float]]:
    if not texts:
        return []
    raw = _raw_embed(texts, model=model, api_key=api_key, base_url=base_url)
    return [_normalize(vec) for vec in raw]
