"""Retrieval micro-benchmark (Phase 3 DoD): recall@k / MRR over seeded clause lookups.

40 deterministic clause-lookup queries derived from the world itself — each has a
*structural* gold answer (the governing (contract, clause) for that artist and intent
as of the window end, resolved from ``label.contracts``/``label.amendments``, never
hand-pinned). The probe runs every query through the real ``search_chunks`` pipeline
in four configurations:

    {artist-scoped, unscoped} x {rerank on, rerank off}

Scoped mode measures ranking the way agents use the tool (governing filter narrows to
one artist); unscoped mode is the hard mode (artist named only in the query text).
Rerank on-vs-off is the §4.4 comparison the README wants.

Offline (keyless CI, model-less sandboxes) the deterministic stack runs:
``--embedder hash --rerank-model lexical``. Real numbers come from
``make retrieval-probe`` on a machine that can load bge-small + the ms-marco
cross-encoder; results are recorded in PHASE_LOG either way, labeled with the stack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg

from backline.config import get_settings
from backline.rag.embedder import Embedder, get_embedder
from backline.rag.reranker import Reranker, get_reranker
from backline.rag.search import search_chunks

AS_OF = date(2026, 6, 30)  # the seeded window's last day
N_QUERIES = 40
TOP_K = 10
RECALL_KS = (1, 3, 5, 10)

Gold = frozenset[tuple[int, str]]  # acceptable (contract_id, clause_no) answers


@dataclass(frozen=True)
class ProbeQuery:
    intent: str
    text: str
    artist_id: int
    gold: Gold


@dataclass
class ModeMetrics:
    n: int = 0
    reciprocal_ranks: list[float] = field(default_factory=list)
    hits_at: dict[int, int] = field(default_factory=lambda: {k: 0 for k in RECALL_KS})

    def add(self, rank: int | None) -> None:
        self.n += 1
        self.reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        for k in RECALL_KS:
            if rank is not None and rank <= k:
                self.hits_at[k] += 1

    def summary(self) -> dict[str, float]:
        return {
            "mrr": round(sum(self.reciprocal_ranks) / max(self.n, 1), 4),
            **{f"recall@{k}": round(self.hits_at[k] / max(self.n, 1), 4) for k in RECALL_KS},
        }


@dataclass(frozen=True)
class _ArtistDeals:
    artist_id: int
    stage_name: str
    bases: tuple[tuple[int, date, date | None], ...]  # (id, from, to) sorted by from
    # base_id -> [(amendment_id, effective_from, replaced_sections)]
    amendments: dict[int, list[tuple[int, date, list[str]]]]

    def current_era(self, as_of: date) -> tuple[int, date, date | None]:
        era = self.bases[0]
        for base in self.bases:
            if base[1] <= as_of:
                era = base
            else:
                break
        return era

    def effective_amendments(
        self, base_id: int, as_of: date, section: str
    ) -> list[tuple[int, date, list[str]]]:
        return sorted(
            (a for a in self.amendments.get(base_id, []) if a[1] <= as_of and section in a[2]),
            key=lambda a: (a[1], a[0]),
        )


async def _load_deals(pool: asyncpg.Pool | asyncpg.Connection) -> dict[int, _ArtistDeals]:
    rows = await pool.fetch(
        """
        SELECT c.id, c.artist_id, c.kind, c.effective_from, c.effective_to,
               a.supersedes_contract_id, a.replaced_sections, ar.stage_name
        FROM label.contracts c
        JOIN label.artists ar ON ar.id = c.artist_id
        LEFT JOIN label.amendments a ON a.amendment_id = c.id
        ORDER BY c.artist_id, c.effective_from, c.id
        """
    )
    bases: dict[int, list[tuple[int, date, date | None]]] = {}
    amendments: dict[int, dict[int, list[tuple[int, date, list[str]]]]] = {}
    names: dict[int, str] = {}
    for row in rows:
        names[row["artist_id"]] = row["stage_name"]
        if row["kind"] == "base":
            bases.setdefault(row["artist_id"], []).append(
                (row["id"], row["effective_from"], row["effective_to"])
            )
        elif row["supersedes_contract_id"] is not None:
            amendments.setdefault(row["artist_id"], {}).setdefault(
                row["supersedes_contract_id"], []
            ).append((row["id"], row["effective_from"], list(row["replaced_sections"])))
    return {
        artist_id: _ArtistDeals(
            artist_id=artist_id,
            stage_name=names[artist_id],
            bases=tuple(base_list),
            amendments=amendments.get(artist_id, {}),
        )
        for artist_id, base_list in bases.items()
    }


def _gold_for_section(
    deals: _ArtistDeals, section: str, base_clause: str, amendment_clause: str
) -> Gold:
    era_id, _, _ = deals.current_era(AS_OF)
    effective = deals.effective_amendments(era_id, AS_OF, section)
    if effective:
        return frozenset({(effective[-1][0], amendment_clause)})
    return frozenset({(era_id, base_clause)})


def _intent_query(intent: str, deals: _ArtistDeals) -> ProbeQuery | None:
    era_id, _era_from, era_to = deals.current_era(AS_OF)
    name = deals.stage_name
    if intent == "royalty_rate":
        return ProbeQuery(
            intent,
            f"What royalty rate does {name} earn on streaming revenue?",
            deals.artist_id,
            _gold_for_section(deals, "royalties", "§3", "§A1"),
        )
    if intent == "recoupment":
        return ProbeQuery(
            intent,
            f"Which costs are recoupable under {name}'s agreement and against which account?",
            deals.artist_id,
            _gold_for_section(deals, "advances_recoupment", "§4", "§A2"),
        )
    if intent == "territory":
        return ProbeQuery(
            intent,
            f"Which territories are excluded from {name}'s deal?",
            deals.artist_id,
            frozenset({(era_id, "§2")}),
        )
    if intent == "cross_collateral":
        return ProbeQuery(
            intent,
            f"Is {name}'s agreement cross-collateralized with their other deals?",
            deals.artist_id,
            frozenset({(era_id, "§6")}),
        )
    if intent == "accounting":
        return ProbeQuery(
            intent,
            f"How often does {name} receive royalty accountings and payments?",
            deals.artist_id,
            frozenset({(era_id, "§5")}),
        )
    if intent == "definitions":
        return ProbeQuery(
            intent,
            f"How does {name}'s agreement define Net Receipts?",
            deals.artist_id,
            frozenset({(era_id, "§1")}),
        )
    if intent == "termination":
        if era_to is None or era_to > AS_OF:
            return None
        return ProbeQuery(
            intent,
            f"What happens to revenue received after {name}'s agreement terminates?",
            deals.artist_id,
            frozenset({(era_id, "§7")}),
        )
    raise ValueError(f"unknown intent {intent!r}")


_INTENT_CYCLE = (
    "royalty_rate",
    "recoupment",
    "territory",
    "cross_collateral",
    "accounting",
    "definitions",
)


async def build_queries(pool: asyncpg.Pool | asyncpg.Connection) -> list[ProbeQuery]:
    """40 deterministic queries: artists by id-stride, intents round-robin, plus the
    seeded special cases (MG, termination) pinned so the hard lookups are always in."""
    deals_by_artist = await _load_deals(pool)
    artist_ids = sorted(deals_by_artist)

    queries: list[ProbeQuery] = []
    mg_artist = await pool.fetchval(
        """
        SELECT c.artist_id FROM label.contract_terms ct
        JOIN label.contracts c ON c.id = ct.contract_id
        WHERE ct.terms -> 'sections' -> 'advances_recoupment'
              ->> 'minimum_guarantee_per_period' IS NOT NULL
        ORDER BY c.artist_id LIMIT 1
        """
    )
    if mg_artist is not None:
        deals = deals_by_artist[mg_artist]
        queries.append(
            ProbeQuery(
                "minimum_guarantee",
                f"Does {deals.stage_name} have a minimum guarantee per accounting period?",
                mg_artist,
                _gold_for_section(deals, "advances_recoupment", "§4", "§A2"),
            )
        )
    terminated = next(
        (
            d
            for _aid, d in sorted(deals_by_artist.items())
            if _intent_query("termination", d) is not None
        ),
        None,
    )
    if terminated is not None:
        query = _intent_query("termination", terminated)
        assert query is not None
        queries.append(query)

    used = {q.artist_id for q in queries}
    stride = max(1, len(artist_ids) // N_QUERIES)
    cursor = 0
    for artist_id in artist_ids[::stride]:
        if len(queries) >= N_QUERIES:
            break
        if artist_id in used:
            continue
        deals = deals_by_artist[artist_id]
        query = None
        for _ in range(len(_INTENT_CYCLE)):
            query = _intent_query(_INTENT_CYCLE[cursor % len(_INTENT_CYCLE)], deals)
            cursor += 1
            if query is not None:
                break
        if query is not None:
            queries.append(query)
            used.add(artist_id)
    if len(queries) < N_QUERIES:  # stride left gaps (skipped artists) — fill densely
        for artist_id in artist_ids:
            if len(queries) >= N_QUERIES:
                break
            if artist_id in used:
                continue
            query = _intent_query(
                _INTENT_CYCLE[cursor % len(_INTENT_CYCLE)], deals_by_artist[artist_id]
            )
            cursor += 1
            if query is not None:
                queries.append(query)
                used.add(artist_id)
    return queries[:N_QUERIES]


async def run_probe(
    pool: asyncpg.Pool | asyncpg.Connection,
    *,
    embedder: Embedder | None,
    reranker: Reranker,
    top_k: int = TOP_K,
) -> dict[str, Any]:
    """Run all queries in all four configurations; return the metrics report."""
    queries = await build_queries(pool)
    metrics: dict[str, ModeMetrics] = {}
    per_query: list[dict[str, Any]] = []

    for query in queries:
        row: dict[str, Any] = {
            "intent": query.intent,
            "text": query.text,
            "artist_id": query.artist_id,
            "gold": sorted(f"{cid}:{clause}" for cid, clause in query.gold),
        }
        for scoped in (True, False):
            for use_rerank in (True, False):
                mode = f"{'scoped' if scoped else 'unscoped'}/{'rerank' if use_rerank else 'fused'}"
                result = await search_chunks(
                    pool,
                    query.text,
                    artist_id=query.artist_id if scoped else None,
                    as_of=AS_OF,
                    top_k=top_k,
                    embedder=embedder,
                    reranker=reranker if use_rerank else None,
                )
                rank = next(
                    (
                        n
                        for n, hit in enumerate(result.hits, start=1)
                        if (hit.contract_id, hit.clause_no) in query.gold
                    ),
                    None,
                )
                metrics.setdefault(mode, ModeMetrics()).add(rank)
                row[mode] = rank
        per_query.append(row)

    stack = {
        "embedder": embedder.id if embedder is not None else "(store default)",
        "reranker": reranker.id,
        "as_of": AS_OF.isoformat(),
        "top_k": top_k,
        "n_queries": len(queries),
    }
    return {
        "stack": stack,
        "modes": {mode: m.summary() for mode, m in sorted(metrics.items())},
        "per_query": per_query,
    }


def format_report(report: dict[str, Any]) -> str:
    stack = report["stack"]
    lines = [
        f"retrieval probe — {stack['n_queries']} clause-lookup queries, "
        f"embedder={stack['embedder']}, reranker={stack['reranker']}, "
        f"as_of={stack['as_of']}",
        "",
        f"{'mode':<18} {'MRR':>6} " + " ".join(f"R@{k:<2}" for k in RECALL_KS),
    ]
    for mode, summary in report["modes"].items():
        recalls = " ".join(f"{summary[f'recall@{k}']:.2f}" for k in RECALL_KS)
        lines.append(f"{mode:<18} {summary['mrr']:6.3f} {recalls}")
    lines.append("")
    on = report["modes"]["scoped/rerank"]["mrr"]
    off = report["modes"]["scoped/fused"]["mrr"]
    lines.append(f"rerank lift (scoped MRR): {off:.3f} → {on:.3f} ({on - off:+.3f})")
    on_u = report["modes"]["unscoped/rerank"]["mrr"]
    off_u = report["modes"]["unscoped/fused"]["mrr"]
    lines.append(f"rerank lift (unscoped MRR): {off_u:.3f} → {on_u:.3f} ({on_u - off_u:+.3f})")
    return "\n".join(lines)


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals.retrieval_probe", description="Measure retrieval quality (§4.4)."
    )
    parser.add_argument(
        "--embedder",
        default=None,
        help="query embedder override ('hash' or a model name); default: the store's model",
    )
    parser.add_argument(
        "--rerank-model",
        default=None,
        help="reranker ('lexical' or a cross-encoder name); default: RERANK_MODEL",
    )
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--json", type=Path, default=None, help="also write the full report")
    args = parser.parse_args(argv)

    settings = get_settings()
    # Cached accessors: one weight load per process. With no --embedder override the
    # store's recorded model decides inside search_chunks — through the same cache,
    # so 160 pipeline passes still load the model once.
    embedder = get_embedder(args.embedder) if args.embedder else None
    reranker = get_reranker(args.rerank_model or settings.rerank_model)

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        report = await run_probe(pool, embedder=embedder, reranker=reranker, top_k=args.top_k)
    finally:
        await pool.close()
    print(format_report(report))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nfull report → {args.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
