"""Retrieval pipeline (BUILD_PLAN §4.4): clause chunks → hybrid search → rerank.

Structured-first: a SQL governing-document filter resolves *which* contracts govern
before any text ranking happens (D-002). See ``chunker``, ``embedder``, ``reranker``,
``governing``, ``search``, and the ``embed`` build job.
"""
