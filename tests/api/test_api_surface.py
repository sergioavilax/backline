"""Phase 6 API surface tests — all keyless, all against the seeded world.

Chat runs the demo scripts (D-024) through the production stack: router run,
agent loop, real tools, tracer sinks, staging writes. The tests assert the SSE
protocol, persistence, the review transitions (including promotion guards), the
span feeds, and the browse endpoints.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

import backline.config
from tests.conftest import requires_postgres

pytestmark = requires_postgres


def read_sse(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    """Collect (event, data) pairs from an SSE body (comments skipped)."""
    events: list[tuple[str, dict[str, Any]]] = []
    current: str | None = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            current = line[len("event: ") :]
        elif line.startswith("data: ") and current is not None:
            events.append((current, json.loads(line[len("data: ") :])))
            current = None
    return events


def make_session(client: TestClient, title: str | None = None) -> str:
    resp = client.post("/sessions", json={"title": title})
    assert resp.status_code == 201
    return str(resp.json()["id"])


def chat(client: TestClient, session_id: str, text: str) -> dict[str, dict[str, Any]]:
    """Send one message; return events keyed by name (last occurrence wins)."""
    with client.stream("POST", f"/sessions/{session_id}/messages", json={"text": text}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = read_sse(response)
    assert events[-1][0] in {"done", "error"}
    return dict(events)


# ── sessions & chat ──────────────────────────────────────────────────────────


def test_session_crud_and_404(api_client: TestClient) -> None:
    session_id = make_session(api_client, title="hello")
    listed = api_client.get("/sessions").json()
    assert any(s["id"] == session_id for s in listed)
    detail = api_client.get(f"/sessions/{session_id}").json()
    assert detail["session"]["title"] == "hello"
    assert detail["messages"] == []
    assert api_client.get(f"/sessions/{uuid.uuid4()}").status_code == 404
    assert (
        api_client.post(f"/sessions/{uuid.uuid4()}/messages", json={"text": "x"}).status_code == 404
    )


def test_chat_counsel_demo_flow(api_client: TestClient) -> None:
    session_id = make_session(api_client)
    events = chat(api_client, session_id, "What is Umbra's streaming royalty rate?")
    assert events["routed"]["target"] == "counsel"
    assert events["routed"]["demo"] is True
    final = events["final"]
    assert final["status"] == "completed"
    assert final["agent"] == "counsel"
    assert final["citations"], "counsel demo must cite a clause"
    assert final["run_id"] == events["run_started"]["run_id"]

    # Both turns persisted; the assistant turn carries the run linkage.
    detail = api_client.get(f"/sessions/{session_id}").json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assistant = detail["messages"][1]["content"]
    assert assistant["run_id"] == final["run_id"]
    assert assistant["route"]["target"] == "counsel"
    # The session titled itself from the first message.
    assert detail["session"]["title"].startswith("What is Umbra")


def test_chat_clarify_flow(api_client: TestClient) -> None:
    session_id = make_session(api_client)
    events = chat(api_client, session_id, "hmm")
    assert events["routed"]["target"] == "clarify"
    assert "clarify" in events
    assert "run_started" not in events
    detail = api_client.get(f"/sessions/{session_id}").json()
    assert detail["messages"][1]["content"]["kind"] == "clarify"


def test_chat_analyst_demo_runs_real_sql(api_client: TestClient) -> None:
    session_id = make_session(api_client)
    events = chat(api_client, session_id, "Top tracks by revenue in 2026-03?")
    final = events["final"]
    assert final["agent"] == "analyst"
    assert final["status"] == "completed"
    # The trace shows a real sql_query tool call.
    spans = api_client.get(f"/runs/{final['run_id']}/spans").json()
    tools = [s["attrs"].get("tool") for s in spans if s["kind"] == "tool_call"]
    assert "sql_query" in tools


# ── reconcile → review queue ─────────────────────────────────────────────────


@pytest.fixture
def proposed_batch(api_client: TestClient) -> dict[str, Any]:
    session_id = make_session(api_client)
    events = chat(api_client, session_id, "Reconcile the 2026-06 statements")
    final = events["final"]
    assert final["agent"] == "reconciler"
    assert final["batch_id"] is not None, "demo reconciler must submit a batch"
    return final


def test_reconciler_submits_and_review_approves(
    api_client: TestClient, proposed_batch: dict[str, Any]
) -> None:
    batch_id = proposed_batch["batch_id"]
    listed = api_client.get("/review/batches").json()
    assert any(b["id"] == batch_id for b in listed)

    detail = api_client.get(f"/review/batches/{batch_id}").json()
    assert detail["batch"]["status"] == "proposed"
    assert detail["batch"]["submitted_by_run"] == proposed_batch["run_id"]
    assert detail["allocations"], "allocations listed for review"
    assert detail["promotion"]["n_paid_artists"] == len(detail["allocations"])
    # Money serializes as strings end to end (invariant 1).
    assert isinstance(detail["allocations"][0]["net_payable"], str)
    for flag in detail["flags"]:
        assert flag["severity"] in {"error", "warning", "info"}

    approved = api_client.post(
        f"/review/batches/{batch_id}/approve", json={"note": "looks right"}
    ).json()
    assert approved["status"] == "approved"
    assert approved["summary"]["review"]["action"] == "approved"
    assert approved["summary"]["review"]["note"] == "looks right"

    # A reviewed batch cannot be reviewed again.
    again = api_client.post(f"/review/batches/{batch_id}/approve", json={})
    assert again.status_code == 409


def test_review_serves_live_agent_shaped_batch(api_client: TestClient) -> None:
    """Phase 6 verification, finding 1 (server half): live-agent batches carry
    JSONB the demo scripts never produce — line_detail money as JSON numbers,
    flag payloads with line_ids lists, numeric-string ids, scalar line_ids, or
    nothing at all. The detail endpoint must serve them (no 500) and resolve
    evidence for every id spelling."""

    async def insert(database_url: str) -> tuple[int, list[int]]:
        conn = await asyncpg.connect(database_url)
        try:
            # The earliest ingested period: long-settled, so no other test's staging
            # or promotion state touches it.
            period: str = await conn.fetchval("SELECT MIN(period) FROM label.statement_lines")
            line_ids = [
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM label.statement_lines WHERE period = $1 ORDER BY id LIMIT 3",
                    period,
                )
            ]
            assert len(line_ids) == 3, "seeded world must have statement lines"
            batch_id: int = await conn.fetchval(
                "INSERT INTO staging.statement_batches (period, summary) "
                "VALUES ($1, $2::jsonb) RETURNING id",
                period,
                json.dumps({"n_allocations": 2}),  # no note, no totals — minimal summary
            )
            for artist_id, line_detail in [
                (1, {"gross": 1204.5678, "recouped": 0, "balance_after": -12.5}),
                (2, {}),
            ]:
                await conn.execute(
                    "INSERT INTO staging.proposed_allocations "
                    "(batch_id, artist_id, period, line_detail, net_payable) "
                    "VALUES ($1, $2, $3, $4::jsonb, 10.5)",
                    batch_id,
                    artist_id,
                    period,
                    json.dumps(line_detail),
                )
            payloads = [
                {"source": "label", "line_ids": line_ids[:2], "observed": 2},
                {"line_id": str(line_ids[2]), "detail": {"observed_gross": 913.4}},
                {"line_ids": line_ids[0]},  # scalar where a list belongs
                {},
            ]
            for payload in payloads:
                await conn.execute(
                    "INSERT INTO staging.flags (batch_id, kind, severity, payload) "
                    "VALUES ($1, 'duplicate_line', 'warning', $2::jsonb)",
                    batch_id,
                    json.dumps(payload),
                )
            return batch_id, line_ids
        finally:
            await conn.close()

    batch_id, line_ids = asyncio.run(insert(backline.config.get_settings().database_url))

    listed = api_client.get("/review/batches").json()
    assert any(b["id"] == batch_id for b in listed)

    response = api_client.get(f"/review/batches/{batch_id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    # Pass-through JSONB arrives verbatim (numbers stay numbers — the UI formats them).
    gross = {a["artist_id"]: a["line_detail"] for a in detail["allocations"]}
    assert gross[1]["gross"] == 1204.5678
    assert gross[2] == {}
    # Evidence resolves for list, numeric-string, and scalar id spellings; never 500s.
    evidence = [f["evidence"] for f in detail["flags"]]
    assert sorted(len(e) for e in evidence) == [0, 1, 1, 2]
    resolved = {line["id"] for flag_evidence in evidence for line in flag_evidence}
    assert resolved == set(line_ids)


def test_reject_requires_note(api_client: TestClient, proposed_batch: dict[str, Any]) -> None:
    batch_id = proposed_batch["batch_id"]
    assert api_client.post(f"/review/batches/{batch_id}/reject", json={}).status_code == 422
    assert (
        api_client.post(f"/review/batches/{batch_id}/reject", json={"note": ""}).status_code == 422
    )
    rejected = api_client.post(
        f"/review/batches/{batch_id}/reject", json={"note": "totals off"}
    ).json()
    assert rejected["status"] == "rejected"
    assert rejected["summary"]["review"]["note"] == "totals off"
    assert api_client.get("/review/batches/999999").status_code == 404


# ── runs & spans ─────────────────────────────────────────────────────────────


def test_runs_listing_and_span_tree(api_client: TestClient) -> None:
    session_id = make_session(api_client)
    events = chat(api_client, session_id, "What is Umbra's download rate?")
    run_id = events["final"]["run_id"]

    runs = api_client.get(f"/runs?session_id={session_id}").json()
    agents = {r["agent"] for r in runs["runs"]}
    assert agents == {"router", "counsel"}, "router and agent runs both traced"

    detail = api_client.get(f"/runs/{run_id}").json()
    assert detail["run"]["status"] == "completed"
    kinds = {s["kind"] for s in detail["spans"]}
    assert {"iteration", "llm_call", "tool_call"} <= kinds
    # Spans nest: every parent_id points at another span of this run.
    ids = {s["id"] for s in detail["spans"]}
    for span in detail["spans"]:
        assert span["parent_id"] is None or span["parent_id"] in ids
    assert api_client.get(f"/runs/{uuid.uuid4()}").status_code == 404


def test_span_stream_replays_finished_run(api_client: TestClient) -> None:
    session_id = make_session(api_client)
    events = chat(api_client, session_id, "Top territories by revenue this quarter")
    run_id = events["final"]["run_id"]
    with api_client.stream("GET", f"/runs/{run_id}/spans/stream") as response:
        assert response.status_code == 200
        stream_events = read_sse(response)
    names = [name for name, _ in stream_events]
    assert names[0] == "snapshot"
    assert names[-1] == "run_end"
    snapshot = stream_events[0][1]
    assert snapshot["run"]["id"] == run_id
    assert snapshot["spans"], "snapshot carries the persisted span tree"


# ── evals ────────────────────────────────────────────────────────────────────


def test_eval_endpoints(api_client: TestClient) -> None:
    baseline = api_client.get("/evals/baseline").json()
    assert baseline["baselines"], "committed baseline served"

    runs = api_client.get("/evals/runs").json()["runs"]
    if runs:  # eval rows exist only if a harness ran against this database
        detail = api_client.get(f"/evals/runs/{runs[0]['id']}").json()
        assert detail["run"]["id"] == runs[0]["id"]
    assert api_client.get(f"/evals/runs/{uuid.uuid4()}").status_code == 404


# ── catalog ──────────────────────────────────────────────────────────────────


def test_catalog_browse(api_client: TestClient) -> None:
    artists = api_client.get("/catalog/artists?q=umbra").json()
    assert artists["total"] >= 1
    artist = artists["artists"][0]
    assert artist["n_tracks"] >= 0

    detail = api_client.get(f"/catalog/artists/{artist['id']}").json()
    assert detail["artist"]["id"] == artist["id"]
    assert detail["contracts"], "every seeded artist has contracts"
    assert detail["contracts"][0]["code"].startswith("FBR-")

    releases = api_client.get("/catalog/releases?limit=5").json()
    assert releases["total"] > 500  # §3.1 scale: ~600 releases
    tracks = api_client.get(f"/catalog/tracks?artist_id={artist['id']}").json()
    assert all(t["primary_artist_id"] == artist["id"] for t in tracks["tracks"])
    assert api_client.get("/catalog/artists/999999").status_code == 404


def test_clause_resolution(api_client: TestClient) -> None:
    # Any artist's first base contract has a §3 (Royalties) clause chunk.
    artists = api_client.get("/catalog/artists?limit=1").json()["artists"]
    detail = api_client.get(f"/catalog/artists/{artists[0]['id']}").json()
    base = next(c for c in detail["contracts"] if c["kind"] == "base")
    clause = api_client.get(f"/catalog/clauses/{base['code']}/3").json()
    assert clause["clause_no"] == "§3"
    assert "royalt" in clause["text"].lower()
    assert clause["contract_id"] == base["id"]

    assert api_client.get("/catalog/clauses/NOT-A-CODE/3").status_code == 422
    assert api_client.get("/catalog/clauses/FBR-C-99999/3").status_code == 404


# ── meta ─────────────────────────────────────────────────────────────────────


def test_meta_reports_demo_mode(api_client: TestClient) -> None:
    meta = api_client.get("/meta").json()
    assert meta["demo_mode"] is True
    assert meta["providers"] == []
    assert meta["world_seed"] == 20260805


# ── fresh-drop promotion (D-025) ─────────────────────────────────────────────


def test_fresh_drop_ingest_approve_promotes(api_client: TestClient) -> None:
    """The Label Engine loop end to end: emit-period drops a new month, the demo
    Reconciler ingests + submits, approval promotes staged lines into label.

    Uses 2026-08 — 2026-07 belongs to the agents/tools suites (they emit it,
    stage its kinetic drop, and re-ingest it), so sharing that period would
    couple this test's promotion to their state and theirs to ours.
    """
    import os
    import subprocess
    import sys

    from tests.conftest import REPO_ROOT

    period = "2026-08"
    emitted = subprocess.run(
        [sys.executable, "-m", "datagen", "emit-period", period],
        cwd=REPO_ROOT,
        env={**os.environ, "DATA_DIR": os.environ["DATA_DIR"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert emitted.returncode == 0, f"emit-period failed:\n{emitted.stdout}\n{emitted.stderr}"

    session_id = make_session(api_client)
    events = chat(api_client, session_id, f"Reconcile the {period} statements")
    final = events["final"]
    assert final["agent"] == "reconciler"
    batch_id = final["batch_id"]
    assert batch_id is not None

    detail = api_client.get(f"/review/batches/{batch_id}").json()
    promote = detail["promotion"]
    assert promote["n_staged_lines"] > 0, "the demo ingested a received drop"
    assert len(promote["statements_to_promote"]) == 1
    statement = promote["statements_to_promote"][0]
    assert statement["status"] == "received"

    approved = api_client.post(f"/review/batches/{batch_id}/approve", json={"note": "ok"})
    assert approved.status_code == 200

    # Promotion happened: statement flipped, staged rows moved into label.
    after = api_client.get(f"/review/batches/{batch_id}").json()["promotion"]
    assert after["statements_to_promote"] == []
    assert after["n_staged_lines"] == 0
