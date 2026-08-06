"""Composite run summaries (D-023): merge targeted category re-runs into one
gate-ready summary under the same identity the regression gate enforces.

The gate keys baseline staleness on ``suite_hash`` (D-016): runs answering the
same committed suite are comparable, whatever harness git sha they ran at. A
diagnose → fix → re-run cycle (D-017..D-022) therefore leaves the latest valid
measurement of each category spread across runs — the full pass plus the
``--categories`` re-runs that superseded parts of it. ``compose_summaries``
merges those summaries, later components overriding earlier ones per whole
category, and refuses anything that would make the result less than a real,
complete run:

- every component must agree on (model, track, subset, suite_hash);
- that suite_hash must match the committed suite being composed against;
- a contributed bucket must carry the subset's full question count for its
  category — partial categories never make it into a baseline;
- the merged set must cover the subset's categories exactly.

Per-category provenance (source run id + git sha) rides in the composed summary
and is folded into the baseline note by ``python -m evals compose`` — the
sanctioned path for multi-run baselines; hand-editing ``baseline.json`` is not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from evals.types import CATEGORIES, Suite

_IDENTITY_FIELDS = ("model", "track", "subset", "suite_hash")


class ComposeError(ValueError):
    """A composite that would be less than a real, complete run."""


def _identity(summary: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(summary["model"]),
        str(summary["track"]),
        str(summary.get("subset") or "full"),
        str(summary["suite_hash"]),
    )


def _short(run_id: object) -> str:
    return str(run_id or "?")[:8]


def _subset_counts(suite: Suite, subset: str) -> dict[str, int]:
    name: Literal["gate", "smoke"] | None
    if subset == "full":
        name = None
    elif subset in ("gate", "smoke"):
        name = "gate" if subset == "gate" else "smoke"
    else:
        raise ComposeError(f"unknown subset {subset!r} — expected full, gate, or smoke")
    counts: dict[str, int] = {}
    for question in suite.subset(name):
        counts[question.category] = counts.get(question.category, 0) + 1
    return counts


def compose_summaries(summaries: Sequence[Mapping[str, Any]], suite: Suite) -> dict[str, Any]:
    """Merge run summaries (oldest first; later override per category) into one."""
    if not summaries:
        raise ComposeError("no summaries to compose")

    identity = _identity(summaries[0])
    for summary in summaries[1:]:
        other = _identity(summary)
        if other == identity:
            continue
        for name, ours, theirs in zip(_IDENTITY_FIELDS, identity, other, strict=True):
            if ours != theirs:
                raise ComposeError(
                    f"summaries disagree on {name}: {ours!r} vs {theirs!r} "
                    f"(run {_short(summary.get('eval_run_id'))}) — a composite must "
                    f"merge runs of one (model, track, subset, suite_hash) shape"
                )
    model, track, subset, shash = identity
    if shash != suite.suite_hash:
        raise ComposeError(
            f"components carry suite_hash {shash} but the committed suite "
            f"{suite.name!r} is {suite.suite_hash} — a baseline must correspond to "
            f"the question set the gate will run against"
        )

    expected = _subset_counts(suite, subset)
    merged: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, str | None]] = {}
    for summary in summaries:
        run_id = str(summary.get("eval_run_id") or "?")
        sha = summary.get("git_sha")
        for category, bucket in summary["categories"].items():
            want = expected.get(category)
            if want is None:
                raise ComposeError(
                    f"category {category!r} (run {_short(run_id)}) is not part of "
                    f"suite {suite.name!r} subset {subset!r}"
                )
            if int(bucket["n"]) != want:
                raise ComposeError(
                    f"{category}: run {_short(run_id)} scored {bucket['n']}/{want} "
                    f"questions — partial categories cannot contribute to a baseline"
                )
            merged[category] = dict(bucket)
            sources[category] = {
                "eval_run_id": run_id,
                "git_sha": str(sha) if sha is not None else None,
            }

    missing = sorted(set(expected) - set(merged))
    if missing:
        raise ComposeError(f"incomplete composite: missing categories {', '.join(missing)}")

    ordered = [category for category in CATEGORIES if category in merged]
    short_ids: list[str] = []
    for summary in summaries:
        short = _short(summary.get("eval_run_id"))
        if short not in short_ids:
            short_ids.append(short)
    total = sum(int(merged[category]["n"]) for category in ordered)
    return {
        "eval_run_id": "composite:" + "+".join(short_ids),
        "suite_hash": shash,
        "model": model,
        "track": track,
        "subset": None if subset == "full" else subset,
        "git_sha": summaries[-1].get("git_sha"),
        "categories": {category: merged[category] for category in ordered},
        "n_questions": total,
        "n_scored": total,
        "sources": {category: sources[category] for category in ordered},
    }


def _source_label(source: Mapping[str, str | None]) -> str:
    return f"{_short(source['eval_run_id'])}@{source['git_sha'] or '?'}"


def provenance_note(composed: Mapping[str, Any]) -> str:
    """One auditable line for the baseline note: which run each category came from."""
    groups: dict[str, list[str]] = {}
    for category, source in composed["sources"].items():
        groups.setdefault(_source_label(source), []).append(category)
    parts = [f"{label}: {', '.join(categories)}" for label, categories in groups.items()]
    return "composed from " + " + ".join(parts)


def render_composite(composed: Mapping[str, Any]) -> str:
    """The composed summary as a table — scores plus per-category provenance."""
    subset = composed.get("subset") or "full"
    lines = [
        f"## Composite — {composed['track']}/{composed['model']} ({subset})",
        "",
        f"- suite `{composed['suite_hash']}` · entry git `{composed.get('git_sha') or '?'}`",
        f"- {provenance_note(composed)}",
        "",
        "| category | n | score | from |",
        "|---|---:|---:|---|",
    ]
    total_n = 0
    weighted = 0.0
    for category, bucket in composed["categories"].items():
        source = _source_label(composed["sources"][category])
        lines.append(f"| {category} | {bucket['n']} | {bucket['score']:.1f} | {source} |")
        total_n += int(bucket["n"])
        weighted += float(bucket["score"]) * int(bucket["n"])
    if total_n:
        lines.append(f"| **overall** | {total_n} | **{weighted / total_n:.1f}** |  |")
    return "\n".join(lines) + "\n"
