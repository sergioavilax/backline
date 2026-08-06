"""The regression gate (§5.4): compare a run's summary against the committed baseline.

Rules, in order:
1. No baseline entry for this (model, track, subset) → **bootstrap pass** (loudly):
   there is nothing to regress against yet; ``--write-baseline`` records one.
2. Baseline exists but its ``suite_hash`` differs → **fail**: the question set moved
   under the baseline; re-baseline consciously (same discipline as the world
   fingerprint golden).
3. Any category present in the baseline but missing from the results → **fail**.
4. Any category score more than ``drop_threshold`` points (default 3.0, §5.4) below
   its baseline → **fail**, listing each drop.
5. Any T2 violation in the results → **fail** (process violations never regress
   quietly — an agent touching ``truth`` or skipping the calculator is not a style
   issue).

``evals/results/baseline.json`` holds one entry per (model, track, subset); entries
are replaced wholesale by ``--write-baseline`` so the file always reflects a real,
complete run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).resolve().parent / "results" / "baseline.json"
DROP_THRESHOLD = 3.0  # §5.4: fail if any category drops more than this many points


@dataclass(frozen=True)
class GateResult:
    passed: bool
    bootstrap: bool
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        if self.bootstrap:
            lines.append("gate: BOOTSTRAP PASS — no baseline entry for this run shape yet.")
            lines.append("      Record one with `python -m evals gate ... --write-baseline`.")
        elif self.passed:
            lines.append("gate: PASS")
        else:
            lines.append("gate: FAIL")
        lines.extend(f"  ✗ {reason}" for reason in self.reasons)
        lines.extend(f"  · {note}" for note in self.notes)
        return "\n".join(lines)


def _key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (entry["model"], entry["track"], entry.get("subset") or "full")


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"baselines": []}
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def find_entry(doc: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any] | None:
    wanted = (summary["model"], summary["track"], summary.get("subset") or "full")
    entries: list[dict[str, Any]] = doc.get("baselines", [])
    for entry in entries:
        if _key(entry) == wanted:
            return entry
    return None


def evaluate_gate(
    summary: dict[str, Any],
    baseline_doc: dict[str, Any],
    *,
    drop_threshold: float = DROP_THRESHOLD,
) -> GateResult:
    entry = find_entry(baseline_doc, summary)
    if entry is None:
        return GateResult(passed=True, bootstrap=True)

    reasons: list[str] = []
    notes: list[str] = []
    if entry["suite_hash"] != summary["suite_hash"]:
        reasons.append(
            f"baseline is stale: suite_hash {entry['suite_hash']} != run "
            f"{summary['suite_hash']} — the question set changed; re-run and "
            f"--write-baseline in the same PR that changed the suite"
        )

    categories = summary["categories"]
    for category, baseline_score in sorted(entry["categories"].items()):
        bucket = categories.get(category)
        if bucket is None:
            reasons.append(f"category {category!r} missing from results")
            continue
        drop = baseline_score - bucket["score"]
        if drop > drop_threshold:
            reasons.append(
                f"{category}: {bucket['score']:.1f} vs baseline {baseline_score:.1f} "
                f"(-{drop:.1f} pts > {drop_threshold:g})"
            )
        elif drop < 0:
            notes.append(f"{category}: improved {baseline_score:.1f} → {bucket['score']:.1f}")

    violations = int(summary.get("t2_violations", 0))
    if violations > 0:
        reasons.append(f"{violations} T2 violation(s) — process assertions failed")

    if summary.get("budget_exhausted"):
        reasons.append(
            f"budget exhausted: only {summary['n_scored']}/{summary['n_questions']} "
            f"questions scored — a partial run cannot clear the gate"
        )

    return GateResult(passed=not reasons, bootstrap=False, reasons=reasons, notes=notes)


def write_baseline(
    summary: dict[str, Any], *, path: Path = BASELINE_PATH, note: str = ""
) -> dict[str, Any]:
    """Upsert this run's category scores as the baseline for its (model, track, subset)."""
    doc = load_baseline(path)
    entry = {
        "model": summary["model"],
        "track": summary["track"],
        "subset": summary.get("subset") or "full",
        "suite_hash": summary["suite_hash"],
        "git_sha": summary.get("git_sha"),
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "categories": {
            category: bucket["score"] for category, bucket in sorted(summary["categories"].items())
        },
        "note": note,
    }
    doc["baselines"] = [
        existing for existing in doc.get("baselines", []) if _key(existing) != _key(entry)
    ] + [entry]
    doc["baselines"].sort(key=_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return entry
