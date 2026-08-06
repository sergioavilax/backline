"""World fingerprint: one hash per table + files, one combined hash.

Three views must agree (and tests assert they do):

1. the in-memory world (pure Python, no DB) — the committed golden file pins this;
2. the same world rebuilt from scratch (determinism);
3. the Postgres content after ``datagen seed`` (DB round-trip fidelity), via
   ``fingerprint_from_db`` — same canonicalization, rows ordered by primary key.

Canonicalization is type-driven and platform-independent: Decimals keep their exact
scale (money is quantized at creation), dates are ISO, JSONB is canonical sorted-key
JSON, booleans are lowercase, NULL is a sentinel.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from backline.jsonutil import canonical_dumps
from datagen.dbload import Record, table_rows
from datagen.worldmodel import World

_NULL = "␀"  # ␀ — never appears in world data
_SEP = "\x1f"

ORDER_BY = {
    "label.artists": "id",
    "label.releases": "id",
    "label.tracks": "id",
    "label.release_tracks": "release_id, track_id",
    "label.contracts": "id",
    "label.contract_terms": "contract_id",
    "label.amendments": "amendment_id",
    "label.advances": "id",
    "label.expenses": "id",
    "label.recoup_accounts": "artist_id, xcollat_group_id",
    "label.distributors": "id",
    "label.statements": "id",
    "label.statement_lines": "id",
    "label.fx_rates": "period, currency",
    "label.dashboard_streams": "period, isrc, store",
    "truth.expected_ledger": "artist_id, period",
    "truth.anomaly_registry": "id",
}


def canon(value: Any) -> str:
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal | int):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, dict):
        return canonical_dumps(value)
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def _hash_records(records: list[Record]) -> str:
    hasher = hashlib.sha256()
    for record in records:
        hasher.update(_SEP.join(canon(v) for v in record).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _normalize_jsonb(text: str) -> str:
    return canonical_dumps(json.loads(text))


def fingerprint_from_world(world: World, fx: list[Record]) -> dict[str, str]:
    tables: dict[str, str] = {}
    for qualified, (_, records) in table_rows(world).items():
        if qualified == "label.fx_rates":
            records = fx
        if qualified == "label.contract_terms":
            # terms are stored as canonical JSON strings already; normalize identically
            records = [(cid, _normalize_jsonb(terms)) for cid, terms in records]
        tables[qualified] = _hash_records(records)
    return tables


async def fingerprint_from_db(database_url: str) -> dict[str, str]:
    conn = await asyncpg.connect(database_url)
    tables: dict[str, str] = {}
    try:
        columns_of = {q: cols for q, (cols, _) in table_rows(World(seed=0)).items()}
        for qualified, order_by in ORDER_BY.items():
            cols = ", ".join(f'"{c}"' for c in columns_of[qualified])
            rows = await conn.fetch(f"SELECT {cols} FROM {qualified} ORDER BY {order_by}")
            records: list[Record] = []
            for row in rows:
                values = list(row)
                if qualified == "label.contract_terms":
                    values[1] = _normalize_jsonb(values[1])
                records.append(tuple(values))
            tables[qualified] = _hash_records(records)
    finally:
        await conn.close()
    return tables


def fingerprint_files(data_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for sub in ("contracts", "inbox"):
        root = data_dir / sub
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            files[str(path.relative_to(data_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def combined_hash(tables: dict[str, str], files: dict[str, str] | None = None) -> str:
    payload: dict[str, Any] = {"tables": tables}
    if files is not None:
        payload["files"] = files
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()
