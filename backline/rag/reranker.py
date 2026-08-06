"""Cross-encoder reranking (§4.4), flag-toggleable, over the fused top-30 only.

- ``CrossEncoderReranker`` — ``cross-encoder/ms-marco-MiniLM-L-6-v2`` on CPU via the
  optional ``embed`` extra; the production path.
- ``LexicalReranker`` — deterministic query-term coverage scoring for keyless CI and
  model-less environments (same role as the hash embedder: an honest offline stand-in,
  and the comparison arm the retrieval probe reports when the real model is absent).

``RERANK=off`` disables the stage entirely; ``search`` treats a ``None`` reranker as
off, so the flag decides at construction time, not per call.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from typing import Protocol

LEXICAL_RERANKER_ID = "lexical-overlap-v1"


class Reranker(Protocol):
    id: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


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


class LexicalReranker:
    """Query-term coverage + bigram bonus. Deterministic, model-free."""

    id = LEXICAL_RERANKER_ID

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        q_tokens = _tokens(query)
        q_set = set(q_tokens)
        q_bigrams = {f"{a} {b}" for a, b in pairwise(q_tokens)}
        scores: list[float] = []
        for text in texts:
            t_tokens = _tokens(text)
            t_set = set(t_tokens)
            coverage = len(q_set & t_set) / len(q_set) if q_set else 0.0
            if q_bigrams:
                t_bigrams = {f"{a} {b}" for a, b in pairwise(t_tokens)}
                coverage += 0.5 * (len(q_bigrams & t_bigrams) / len(q_bigrams))
            scores.append(coverage)
        return scores


class CrossEncoderReranker:
    """ms-marco MiniLM cross-encoder (or compatible) via sentence-transformers, CPU."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as error:
            raise RuntimeError(
                f"reranker {model_name!r} needs sentence-transformers — install the "
                f"'embed' extra (uv sync --extra embed) or set RERANK_MODEL=lexical for "
                f"the deterministic offline reranker"
            ) from error
        self.id = model_name
        self._model = CrossEncoder(model_name, device="cpu")

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        scores = self._model.predict([(query, text) for text in texts])
        return [float(s) for s in scores]


def build_reranker(spec: str) -> Reranker:
    if spec == "lexical" or spec == LEXICAL_RERANKER_ID:
        return LexicalReranker()
    return CrossEncoderReranker(spec)
