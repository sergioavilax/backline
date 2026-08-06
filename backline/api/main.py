"""Backline API service (BUILD_PLAN Phase 6).

The full product surface over the platform: sessions + SSE chat, runs + the live
span stream, the Review Queue's approve/reject (the human half of invariant 5),
eval results, and catalog browsing. Assembly happens once in the lifespan
(``AppState``); with no provider configured the API serves the keyless demo mode
(D-024) so a cold clone still demos everything.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from backline import __version__
from backline.api.routes import catalog, evals, review, runs, sessions
from backline.api.schemas import MetaOut
from backline.api.state import AppState, close_state, create_state, get_state
from backline.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = await create_state()
    app.state.backline = state
    try:
        yield
    finally:
        await close_state(state)


app = FastAPI(
    title="Backline API",
    version=__version__,
    description=(
        "Agent platform for music label operations — contracts, catalog, royalty "
        "statements. Agents propose; humans approve. Everything is traced."
    ),
    lifespan=lifespan,
)

# The UI is a separate origin (Next.js on :3000, API on :8000). The API carries no
# credentials or cookies — origin-open CORS is deliberate for this surface.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions.router)
app.include_router(runs.router)
app.include_router(review.router)
app.include_router(evals.router)
app.include_router(catalog.router)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving."""
    return {"status": "ok", "service": "api", "version": __version__}


@app.get("/readyz", tags=["health"])
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness: the API can reach Postgres."""
    settings = get_settings()
    try:
        conn = await asyncpg.connect(settings.database_url, timeout=5)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        response.status_code = 503
        return {"status": "unavailable", "database": "unreachable", "detail": str(exc)}
    return {"status": "ok", "database": "ok"}


@app.get("/meta", response_model=MetaOut, tags=["health"])
async def meta(request: Request) -> MetaOut:
    """How this instance is assembled — the UI labels demo mode from this."""
    state: AppState = get_state(request)
    return MetaOut(
        version=__version__,
        demo_mode=state.demo_mode,
        providers=sorted(state.providers),
        planner_model=state.settings.planner_model,
        utility_model=state.settings.utility_model,
        router_model=state.settings.router_model,
        world_seed=state.settings.world_seed,
    )
