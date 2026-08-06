"""Embedder contract tests (keyless — the deterministic hash embedder only).

The real bge-small model can't run in keyless CI; what these tests pin is the contract
every embedder must honor (dim, normalization, determinism) plus the lexical-similarity
sanity that makes the hash embedder a *meaningful* offline stand-in rather than noise.
"""

import math

import pytest

from backline.rag.embedder import HashingEmbedder, build_embedder, get_embedder
from backline.rag.reranker import LexicalReranker, get_reranker


def norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_dim_and_unit_norm() -> None:
    emb = HashingEmbedder()
    [vec] = emb.encode_passages(["streaming royalties are thirty percent"])
    assert len(vec) == 384
    assert norm(vec) == pytest.approx(1.0, abs=1e-9)
    assert norm(emb.encode_query("royalty rate")) == pytest.approx(1.0, abs=1e-9)


def test_deterministic_across_instances() -> None:
    a = HashingEmbedder().encode_passages(["cross-collateralization of recoupment accounts"])
    b = HashingEmbedder().encode_passages(["cross-collateralization of recoupment accounts"])
    assert a == b


def test_empty_text_yields_zero_vector() -> None:
    [vec] = HashingEmbedder().encode_passages([""])
    assert norm(vec) == 0.0


def test_lexical_similarity_orders_sensibly() -> None:
    emb = HashingEmbedder()
    query = emb.encode_query("What royalty rate applies to streaming revenue?")
    royalty, unrelated = emb.encode_passages(
        [
            "Label shall credit Artist's royalty account with 30% of Net Receipts from "
            "interactive audio streaming throughout the Territory.",
            "This Agreement shall be governed by the laws of the State of New York.",
        ]
    )
    assert cosine(query, royalty) > cosine(query, unrelated)


def test_query_and_passage_share_a_space() -> None:
    emb = HashingEmbedder()
    q = emb.encode_query("minimum guarantee per accounting period")
    [p] = emb.encode_passages(["minimum guarantee per accounting period"])
    assert cosine(q, p) == pytest.approx(1.0, abs=1e-9)


def test_build_embedder_resolves_hash() -> None:
    emb = build_embedder("hash")
    assert isinstance(emb, HashingEmbedder)
    assert emb.dim == 384
    assert emb.id == "hash-bow-384-v1"


def test_build_embedder_real_model_requires_extra() -> None:
    try:
        import sentence_transformers  # noqa: F401

        pytest.skip("sentence-transformers installed — the lazy-import error path is moot")
    except ImportError:
        pass
    with pytest.raises(RuntimeError, match="embed"):
        build_embedder("BAAI/bge-small-en-v1.5")


def test_get_embedder_caches_one_instance_per_spec() -> None:
    """Model weights must load once per process, not once per query (a real
    sentence-transformers load costs seconds; the retrieval probe alone makes 160
    pipeline passes)."""
    get_embedder.cache_clear()
    a = get_embedder("hash")
    b = get_embedder("hash")
    assert a is b
    assert isinstance(a, HashingEmbedder)
    info = get_embedder.cache_info()
    assert (info.misses, info.hits) == (1, 1)
    # A distinct spec is a distinct cache entry, not a collision.
    assert get_embedder("hash-bow-384-v1") is not a


def test_get_reranker_caches_one_instance_per_spec() -> None:
    get_reranker.cache_clear()
    a = get_reranker("lexical")
    b = get_reranker("lexical")
    assert a is b
    assert isinstance(a, LexicalReranker)
    info = get_reranker.cache_info()
    assert (info.misses, info.hits) == (1, 1)


def test_build_embedder_stays_uncached() -> None:
    assert build_embedder("hash") is not build_embedder("hash")
