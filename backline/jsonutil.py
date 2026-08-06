"""The one JSON encoder for Decimal-bearing structures (BUILD_PLAN §9).

Decimals serialize as strings — a JSON float can silently corrupt money, so nothing in
this repo ever emits one for a monetary value. UUIDs serialize as their canonical string
form and datetimes as ISO-8601 (trace records carry both). Use ``canonical_dumps``
wherever a dict must serialize reproducibly (JSONB columns, fingerprints, trace lines).
"""

import datetime as _dt
import json
import uuid
from decimal import Decimal
from typing import Any


class DecimalSafeEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, _dt.datetime | _dt.date):
            return o.isoformat()
        return super().default(o)


def canonical_dumps(obj: Any) -> str:
    """Deterministic JSON: sorted keys, tight separators, Decimals as strings."""
    return json.dumps(
        obj, cls=DecimalSafeEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
