"""The committed OpenAPI schema must match the live route definitions.

Keyless and database-free: the schema derives from the FastAPI app object. On
drift, regenerate with ``make openapi`` and commit the diff — the schema is
documentation, and documentation that lies fails CI.
"""

from __future__ import annotations

import json

from backline.api.export import OPENAPI_PATH, render_openapi


def test_committed_openapi_matches_routes() -> None:
    assert OPENAPI_PATH.is_file(), "docs/api/openapi.json missing — run `make openapi`"
    committed = OPENAPI_PATH.read_text(encoding="utf-8")
    assert committed == render_openapi(), (
        "OpenAPI schema drifted from the routes — regenerate with `make openapi` "
        "and commit the result"
    )


def test_openapi_covers_the_planned_surface() -> None:
    """BUILD_PLAN Phase 6 names the route families; the schema must carry them."""
    doc = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    paths = set(doc["paths"])
    for expected in (
        "/sessions",
        "/sessions/{session_id}/messages",
        "/runs",
        "/runs/{run_id}/spans",
        "/runs/{run_id}/spans/stream",
        "/review/batches",
        "/review/batches/{batch_id}/approve",
        "/review/batches/{batch_id}/reject",
        "/evals/runs",
        "/evals/baseline",
        "/catalog/artists",
        "/catalog/clauses/{code}/{clause_no}",
        "/healthz",
        "/readyz",
        "/meta",
    ):
        assert expected in paths, f"OpenAPI schema lost {expected}"
