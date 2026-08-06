"""Application state: the one place the API assembles platform dependencies.

Built once in the FastAPI lifespan; request handlers reach it through
``get_state(request)``. Providers follow the same policy as ``scripts/ask.py``: real
providers when keys/endpoints are configured, otherwise the API runs in **demo mode**
(D-024) — chat drives MockProvider scripts through the real router/runtime/tools so
the whole surface works on a keyless cold clone.

Startup is resilient: an unreachable database does not kill the process — health
endpoints keep serving (``/readyz`` says 503) and data routes raise 503 on access.
That mirrors the compose story, where the API may briefly outrun the db container.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
from fastapi import HTTPException, Request

from backline.config import Settings, get_settings
from backline.core.trace import JsonlSink, PostgresSink, TracePubSub, Tracer, TraceSink
from backline.providers.anthropic import AnthropicProvider
from backline.providers.base import Provider
from backline.providers.openai_compat import OpenAICompatProvider
from backline.providers.registry import ModelRegistry
from backline.tools.context import ToolContext


def jload(value: object) -> Any:
    """Decode one JSONB column value read through the shared pool.

    The pool deliberately keeps asyncpg's default codec (JSONB in/out as ``str``):
    the runtime tools share this pool and already write with ``canonical_dumps`` +
    ``::jsonb`` casts — a custom codec here would double-encode their writes. API
    read sites decode explicitly with this helper instead.
    """
    if isinstance(value, str | bytes):
        return json.loads(value)
    return value


def build_providers(settings: Settings) -> dict[str, Provider]:
    """Real providers from configuration; empty dict = demo mode."""
    providers: dict[str, Provider] = {}
    if settings.anthropic_api_key:
        providers["anthropic"] = AnthropicProvider(api_key=settings.anthropic_api_key)
    if settings.openai_compat_base_url:
        providers["openai_compat"] = OpenAICompatProvider(
            base_url=settings.openai_compat_base_url,
            api_key=settings.openai_compat_api_key or None,
        )
    return providers


class AppState:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ModelRegistry,
        tracer: Tracer,
        pubsub: TracePubSub,
        postgres_sink: PostgresSink,
        providers: dict[str, Provider],
        pool: asyncpg.Pool | None,
        db_error: str | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.tracer = tracer
        self.pubsub = pubsub
        self.postgres_sink = postgres_sink
        self.providers = providers
        self._pool = pool
        self.db_error = db_error
        self._tool_context: ToolContext | None = None
        # Chat runs execute in background tasks so a dropped SSE client never kills
        # a run mid-flight (a half-submitted batch would be worse than a lost stream).
        self.chat_tasks: set[asyncio.Task[None]] = set()

    @property
    def demo_mode(self) -> bool:
        return not self.providers

    @property
    def pool(self) -> asyncpg.Pool:
        """The connection pool, or a clean 503 when the database never came up."""
        if self._pool is None:
            raise HTTPException(status_code=503, detail=f"database unavailable: {self.db_error}")
        return self._pool

    @property
    def tool_context(self) -> ToolContext:
        if self._tool_context is None:
            self._tool_context = ToolContext.create(self.pool)
        return self._tool_context

    def track(self, task: asyncio.Task[None]) -> None:
        self.chat_tasks.add(task)
        task.add_done_callback(self.chat_tasks.discard)


async def create_state() -> AppState:
    settings = get_settings()
    pool: asyncpg.Pool | None = None
    db_error: str | None = None
    try:
        pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=8, timeout=10)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        db_error = f"{type(exc).__name__}: {exc}"
    pubsub = TracePubSub()
    postgres_sink = PostgresSink(settings.database_url)
    sinks: list[TraceSink] = [
        postgres_sink,
        JsonlSink(settings.data_path / "traces"),
        pubsub,
    ]
    return AppState(
        settings=settings,
        registry=ModelRegistry.load(),
        tracer=Tracer(sinks),
        pubsub=pubsub,
        postgres_sink=postgres_sink,
        providers=build_providers(settings),
        pool=pool,
        db_error=db_error,
    )


async def close_state(state: AppState) -> None:
    if state.chat_tasks:
        await asyncio.gather(*state.chat_tasks, return_exceptions=True)
    await state.postgres_sink.aclose()
    if state._pool is not None:
        await state._pool.close()


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.backline
    return state
