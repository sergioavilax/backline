"""Eval Dashboard data (§6 surface 4): runs, per-question results, the baseline.

Reads what the Phase 5 harness wrote (``app.eval_runs`` / ``app.eval_results``) plus
the committed regression baseline. ``eval_results.detail.run_id`` is the drill-down
hook: a failed question links straight to its full trace in the Trace Inspector.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from backline.api.schemas import (
    BaselineOut,
    EvalListOut,
    EvalResultOut,
    EvalRunDetailOut,
    EvalRunOut,
)
from backline.api.state import AppState, get_state, jload
from backline.config import repo_root

router = APIRouter(prefix="/evals", tags=["evals"])

State = Annotated[AppState, Depends(get_state)]

_BASELINE_PATH = repo_root() / "evals" / "results" / "baseline.json"


def _eval_run_out(row: object) -> EvalRunOut:
    data = dict(row)  # type: ignore[call-overload]  # asyncpg.Record is mapping-like
    return EvalRunOut(**{**data, "summary": jload(data["summary"])})


@router.get("/runs", response_model=EvalListOut)
async def list_eval_runs(state: State, limit: int = 50) -> EvalListOut:
    rows = await state.pool.fetch(
        "SELECT id, suite_hash, model, git_sha, started_at, finished_at, summary "
        "FROM app.eval_runs ORDER BY started_at DESC LIMIT $1",
        min(limit, 200),
    )
    return EvalListOut(runs=[_eval_run_out(r) for r in rows])


@router.get("/runs/{eval_run_id}", response_model=EvalRunDetailOut)
async def get_eval_run(eval_run_id: uuid.UUID, state: State) -> EvalRunDetailOut:
    row = await state.pool.fetchrow(
        "SELECT id, suite_hash, model, git_sha, started_at, finished_at, summary "
        "FROM app.eval_runs WHERE id = $1",
        eval_run_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"no eval run {eval_run_id}")
    results = await state.pool.fetch(
        "SELECT question_id, tier, score, passed, detail FROM app.eval_results "
        "WHERE eval_run_id = $1 ORDER BY question_id, tier",
        eval_run_id,
    )
    return EvalRunDetailOut(
        run=_eval_run_out(row),
        results=[EvalResultOut(**{**dict(r), "detail": jload(r["detail"])}) for r in results],
    )


@router.get("/baseline", response_model=BaselineOut)
async def get_baseline() -> BaselineOut:
    """The committed regression baseline (what the CI gate compares against)."""
    if not _BASELINE_PATH.is_file():
        return BaselineOut(baselines=[])
    return BaselineOut(**json.loads(_BASELINE_PATH.read_text(encoding="utf-8")))
