"""Semantic search embeddings (feature 14).

Uses a lightweight ONNX sentence-transformer (via `fastembed`) so it runs on a
Raspberry Pi without PyTorch. If the model is unavailable (offline / first run),
the search layer falls back to FTS5 keyword matching, so the system keeps working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from typing import Any

log = logging.getLogger("butler.embed")


class Embedder:
    def __init__(self, model: str):
        self.model = model
        self._em: Any = None
        self._ready = False
        self._mem: dict[str, list[float]] = {}
        self._mem_limit = 512

    def _load(self) -> Any:
        if self._em is None:
            try:
                from fastembed import TextEmbedding
                self._em = TextEmbedding(model_name=self.model,
                                         cache_dir=None)
                self._ready = True
            except Exception as exc:  # pragma: no cover - offline path
                log.warning("Embedding model unavailable (%s)", exc)
                self._em = None
                self._ready = False
        return self._em

    @property
    def ready(self) -> bool:
        self._load()
        return self._ready

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._load()
        if not self._ready or not texts:
            return []
        try:
            out = list(self._em.embed(texts))
        except Exception as exc:
            log.warning("embed failed: %s", exc)
            return []
        return [list(map(float, v)) for v in out]

    def embed_one(self, text: str) -> list[float] | None:
        if not text.strip():
            return None
        key = hashlib.md5(text.encode()).hexdigest()
        if key in self._mem:
            return self._mem[key]
        vecs = self.embed([text])
        if not vecs:
            return None
        if len(self._mem) > self._mem_limit:
            self._mem.clear()
        self._mem[key] = vecs[0]
        return vecs[0]


def mean_pool(vectors: list[list[float]], dim: int) -> list[float]:
    if not vectors:
        return [0.0] * dim
    acc = vectors[0][:dim]
    for v in vectors[1:]:
        for i in range(dim):
            acc[i] += v[i]
    return [x / len(vectors) for x in acc]


def normalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
