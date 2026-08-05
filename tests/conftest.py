import os

import pytest

requires_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — Postgres-backed tests run in CI (service container) "
    "or against `docker compose up` locally",
)
