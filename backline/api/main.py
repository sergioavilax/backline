"""Backline API service.

Phase 0 surface: health endpoints only. Sessions/runs/review/evals routes land in Phase 6.
"""

from typing import Any

import asyncpg
from fastapi import FastAPI, Response

from backline import __version__
from backline.config import get_settings

app = FastAPI(title="Backline API", version=__version__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving."""
    return {"status": "ok", "service": "api", "version": __version__}


@app.get("/readyz")
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
