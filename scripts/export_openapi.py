"""Write the committed OpenAPI schema: ``docs/api/openapi.json``.

The schema is documentation — the API contract reviewers can read without booting
the stack. ``tests/api/test_openapi.py`` fails when the routes drift from the
committed file; regenerate with:

    make openapi
"""

import sys


def main() -> int:
    from backline.api.export import OPENAPI_PATH, render_openapi

    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(render_openapi(), encoding="utf-8")
    print(f"wrote {OPENAPI_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
