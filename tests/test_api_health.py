import pytest
from fastapi.testclient import TestClient

import backline.config
from backline.api.main import app


def test_healthz_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "api"


def test_readyz_reports_unreachable_db_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point at a port nothing listens on: readiness must degrade to 503, not crash.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nobody@127.0.0.1:59999/nodb")
    backline.config.get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"
    finally:
        backline.config.get_settings.cache_clear()
