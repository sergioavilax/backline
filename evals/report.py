"""Report builder (§5): per-category tables + markdown export for README/PHASE_LOG.

Works from summary dicts (``summary.json`` / ``app.eval_runs.summary``), so reports
render for any past run without re-scoring. ``render_compare`` lines tracks/models up
side by side — the §5.3 headline (B0 vs B1 vs platform accuracy by category)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.types import CATEGORIES

_TIER_ORDER = ("t1", "t2", "t3")


def load_summary(path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if "categories" not in raw:
        raise ValueError(f"{path} is not an eval summary (no categories)")
    return raw


def _fmt_score(value: float | None) -> str:
    return "—" if value is None else f"{value:5.1f}"


def _label(summary: dict[str, Any]) -> str:
    subset = summary.get("subset") or "full"
    return f"{summary['track']}/{summary['model']} ({subset})"


def render_markdown(summary: dict[str, Any]) -> str:
    """One run → a category x tier markdown table plus run metadata."""
    lines = [
        f"## Eval results — {_label(summary)}",
        "",
        f"- suite `{summary['suite_hash']}` · git `{summary.get('git_sha') or '?'}` · "
        f"run `{summary['eval_run_id']}`",
        f"- {summary['n_scored']}/{summary['n_questions']} questions scored · "
        f"spend ${summary['total_cost_usd']} (budget ${summary['budget_usd']}"
        + (", exhausted" if summary.get("budget_exhausted") else "")
        + f") · latency p50 {summary['latency_ms_p50']}ms / p95 {summary['latency_ms_p95']}ms",
        f"- T2 violations: {summary['t2_violations']}"
        + (
            f" · judge: {summary['judge']['model']} (rubric {summary['judge']['rubric_sha256']})"
            if summary.get("judge")
            else ""
        ),
        "",
        "| category | n | score | T1 | T2 | T3 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    categories = summary["categories"]
    total_n = 0
    weighted = 0.0
    for category in CATEGORIES:
        bucket = categories.get(category)
        if bucket is None:
            continue
        tiers = bucket.get("tiers", {})
        lines.append(
            f"| {category} | {bucket['n']} | {bucket['score']:.1f} | "
            + " | ".join(_fmt_score(tiers.get(tier)) for tier in _TIER_ORDER)
            + " |"
        )
        total_n += bucket["n"]
        weighted += bucket["score"] * bucket["n"]
    if total_n:
        lines.append(f"| **overall** | {total_n} | **{weighted / total_n:.1f}** |  |  |  |")
    return "\n".join(lines) + "\n"


def render_compare(summaries: list[dict[str, Any]]) -> str:
    """Several runs, one table: category rows x run columns (the baselines chart)."""
    if not summaries:
        return "(no summaries)\n"
    labels = [_label(s) for s in summaries]
    lines = [
        "## Eval comparison — accuracy by category",
        "",
        "| category | " + " | ".join(labels) + " |",
        "|---|" + "---:|" * len(summaries),
    ]
    for category in CATEGORIES:
        row: list[str] = [category]
        present = False
        for summary in summaries:
            bucket = summary["categories"].get(category)
            row.append("—" if bucket is None else f"{bucket['score']:.1f}")
            present = present or bucket is not None
        if present:
            lines.append("| " + " | ".join(row) + " |")
    totals = []
    for summary in summaries:
        n = sum(b["n"] for b in summary["categories"].values())
        weighted = sum(b["score"] * b["n"] for b in summary["categories"].values())
        totals.append(f"**{weighted / n:.1f}**" if n else "—")
    lines.append("| **overall** | " + " | ".join(totals) + " |")
    lines.append("")
    lines.append("| run | spend | p50 | p95 | T2 violations |\n|---|---:|---:|---:|---:|")
    for label, summary in zip(labels, summaries, strict=True):
        lines.append(
            f"| {label} | ${summary['total_cost_usd']} | {summary['latency_ms_p50']}ms "
            f"| {summary['latency_ms_p95']}ms | {summary['t2_violations']} |"
        )
    return "\n".join(lines) + "\n"
