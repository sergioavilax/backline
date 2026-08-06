"""Render the committed OpenAPI schema (docs/api/openapi.json).

Kept inside the package so both the export script and the drift test import one
implementation. The schema derives from the route definitions alone — no server,
no database.
"""

import json

from backline.config import repo_root

OPENAPI_PATH = repo_root() / "docs" / "api" / "openapi.json"


def render_openapi() -> str:
    from backline.api.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
