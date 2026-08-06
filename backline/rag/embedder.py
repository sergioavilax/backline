"""Embedders for the hybrid retrieval stack (§4.4). One vector space per store.

Two implementations of one contract (384-dim, L2-normalized):

- ``SentenceTransformerEmbedder`` — the production path: ``BAAI/bge-small-en-v1.5``
  on CPU via sentence-transformers (the optional ``embed`` extra). Queries get the
  bge retrieval prefix; passages don't.
- ``HashingEmbedder`` — deterministic feature-hashed bag-of-words (unigrams+bigrams,
  signed sha256 buckets, sublinear tf). No model, no network, identical everywhere —
  it is the embedder for keyless CI/tests and model-less environments, and an honest
  lexical-similarity baseline rather than noise.

The chunk store records which embedder produced each vector (``embedding_model``);
queries must embed with the same one — ``search`` enforces that, this module just
builds embedders by spec.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise
from typing import Protocol

EMBEDDING_DIM = 384

HASH_EMBEDDER_ID = "hash-bow-384-v1"
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    id: str
    dim: int

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


class HashingEmbedder:
    """sha256 feature hashing → signed 384-bucket bag-of-words, L2-normalized."""

    id = HASH_EMBEDDER_ID
    dim = EMBEDDING_DIM

    def _encode(self, text: str) -> list[float]:
        tokens = _tokens(text)
        features = Counter(tokens)
        features.update(f"{a} {b}" for a, b in pairwise(tokens))
        vec = [0.0] * self.dim
        for feature, count in features.items():
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode(t) for t in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._encode(text)


class SentenceTransformerEmbedder:
    """bge-small-en-v1.5 (or compatible) via sentence-transformers, CPU."""

    dim = EMBEDDING_DIM

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                f"embedder {model_name!r} needs sentence-transformers — install the "
                f"'embed' extra (uv sync --extra embed) or set EMBED_MODEL=hash for the "
                f"deterministic offline embedder"
            ) from error
        self.id = model_name
        self._model = SentenceTransformer(model_name, device="cpu")
        model_dim = self._model.get_sentence_embedding_dimension()
        if model_dim != self.dim:
            raise RuntimeError(
                f"embedder {model_name!r} produces {model_dim}-dim vectors; the chunk "
                f"store is fixed at {self.dim} (migration 0003)"
            )

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        return [[float(x) for x in vector] for vector in vectors]

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts)

    def encode_query(self, text: str) -> list[float]:
        return self._encode([_BGE_QUERY_PREFIX + text])[0]


def build_embedder(spec: str) -> Embedder:
    """``"hash"`` → the deterministic embedder; anything else → a sentence-transformers
    model name (raises with guidance when the extra isn't installed)."""
    if spec == "hash" or spec == HASH_EMBEDDER_ID:
        return HashingEmbedder()
    return SentenceTransformerEmbedder(spec)


def vector_literal(vector: Sequence[float]) -> str:
    """Render a vector as pgvector's text form (queries pass it with a ``::vector`` cast)."""
    return "[" + ",".join(f"{x:.8g}" for x in vector) + "]"
