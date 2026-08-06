"""Shared dependencies for tool construction.

Tools are pure ``Tool[P]`` bindings (name, schema, handler); everything stateful they
need — the connection pool, settings, and optional retrieval-stack overrides — arrives
through one ``ToolContext`` built at startup (or per test). Handlers close over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncpg

from backline.config import Settings, get_settings

if TYPE_CHECKING:
    from backline.rag.embedder import Embedder
    from backline.rag.reranker import Reranker


@dataclass
class ToolContext:
    """What every tool factory receives.

    ``embedder``/``reranker`` are optional overrides: when None, the retrieval tools
    resolve them from settings and the chunk store's recorded embedding model (the
    stored corpus decides which embedder queries must use).
    """

    pool: asyncpg.Pool
    settings: Settings
    embedder: Embedder | None = None
    reranker: Reranker | None = None

    @classmethod
    def create(cls, pool: asyncpg.Pool) -> ToolContext:
        return cls(pool=pool, settings=get_settings())
