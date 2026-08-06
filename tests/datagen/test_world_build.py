"""World structure assertions + the determinism golden (pure Python — no DB, no disk).

The committed golden file pins the world's content hash for ``WORLD_SEED=20260805``:
any change to generation logic, config, or dependency behavior that would silently move
the answer key fails here first. Regenerate deliberately with
``python -m datagen fingerprint --files > tests/golden/world_fingerprint.json`` after an
*intentional* world change (and say so in the PR).
"""

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from backline.royaltycalc import parse_terms_doc, resolve_terms
from datagen.assemble import BuiltWorld
from datagen.config import WorldConfig
from datagen.dbload import fx_rows
from datagen.fingerprint import combined_hash, fingerprint_from_world
from datagen.pdfrender import contract_document, document_text

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "world_fingerprint.json"


class TestScale:
    def test_headline_counts(self, built: BuiltWorld) -> None:
        world = built.world
        assert len(world.artists) == 150
        base = [c for c in world.contracts if c.kind == "base"]
        amendments = [c for c in world.contracts if c.kind == "amendment"]
        assert 280 <= len(base) <= 360  # ~320
        assert 70 <= len(amendments) <= 110  # ~90
        assert 520 <= len(world.releases) <= 680  # ~600
        assert 2100 <= len(world.tracks) <= 2700  # ~2,400
        assert len(world.statement_lines) >= 450_000  # the §3.1 floor
        assert len(world.statements) == 72  # 6 feeds x 12 periods
        assert all(s.status == "ingested" for s in world.statements)

    def test_answer_key_covers_every_artist_period(self, built: BuiltWorld) -> None:
        assert len(built.world.ledger) == 150 * 12
        pairs = {(row.artist_id, row.period) for row in built.world.ledger}
        assert len(pairs) == 150 * 12

    def test_every_statement_has_lines(self, built: BuiltWorld) -> None:
        by_statement = Counter(line.statement_id for line in built.world.statement_lines)
        for statement in built.world.statements:
            assert by_statement[statement.id] > 0

    def test_advances_and_expenses_land_in_window(self, built: BuiltWorld) -> None:
        world = built.world
        assert len(world.advances) == 100
        assert len(world.expenses) == 80
        lo, hi = date(2025, 7, 1), date(2026, 6, 30)
        assert all(lo <= a.granted_at <= hi for a in world.advances)
        assert all(lo <= e.incurred_at <= hi for e in world.expenses)
        # marketing is the non-recoupable carve-out class
        assert all(not e.recoupable for e in world.expenses if e.expense_class == "marketing")


class TestAnomalies:
    def test_registry_counts(self, built: BuiltWorld) -> None:
        registry = built.world.anomalies
        assert len(registry) == 40
        per_kind = Counter(a.kind for a in registry)
        for kind in (
            "duplicate_line",
            "unknown_isrc",
            "currency_mismatch",
            "negative_units",
            "dashboard_gap",
            "period_bleed",
            "sudden_territory_spike",
        ):
            assert per_kind[kind] >= 3, f"{kind}: {per_kind[kind]} < 3"
        borderline = [a for a in registry if a.expected_flag_kind is None]
        assert len(borderline) == 2

    def test_registry_drives_corruption_not_vice_versa(self, built: BuiltWorld) -> None:
        # Every registered anomaly points at a line that exists in the dirty set.
        dirty_ids = {line.id for line in built.world.statement_lines}
        for entry in built.world.anomalies:
            assert entry.statement_line_id in dirty_ids, entry

    def test_clean_vs_dirty_delta_is_exactly_the_injections(self, built: BuiltWorld) -> None:
        # 6 dups + 6 unknown + 5 negative + 5 bleed + 5 spikes = 27 injected lines;
        # the borderline spike lives in both sets; currency_mismatch mutates in place.
        assert len(built.world.statement_lines) - len(built.world.clean_lines) == 27

    def test_duplicate_lines_share_line_hash(self, built: BuiltWorld) -> None:
        by_id = {line.id: line for line in built.world.statement_lines}
        dups = [a for a in built.world.anomalies if a.kind == "duplicate_line"]
        hashes = Counter(line.line_hash for line in built.world.statement_lines)
        for entry in dups:
            assert hashes[by_id[entry.statement_line_id].line_hash] >= 2

    def test_unknown_isrcs_are_absent_from_catalog(self, built: BuiltWorld) -> None:
        catalog = {t.isrc for t in built.world.tracks}
        by_id = {line.id: line for line in built.world.statement_lines}
        for entry in built.world.anomalies:
            if entry.kind == "unknown_isrc":
                assert by_id[entry.statement_line_id].isrc not in catalog

    def test_negative_units_never_reach_the_clean_set(self, built: BuiltWorld) -> None:
        assert all(line.units >= 0 for line in built.world.clean_lines)
        assert any(line.units < 0 for line in built.world.statement_lines)


class TestSpecialCases:
    def test_cross_collateralized_artists(self, built: BuiltWorld) -> None:
        special = built.structure.special
        assert len(special.xcollat_artist_ids) == 12
        for artist_id in special.xcollat_artist_ids:
            contracts = built.structure.eras[artist_id]
            assert len(contracts) >= 2
            accounts = {
                c.terms_json["sections"]["advances_recoupment"]["account"] for c in contracts
            }
            assert accounts == {f"XC-{artist_id:04d}"}  # one pooled account

    def test_minimum_guarantee_contract(self, built: BuiltWorld) -> None:
        special = built.structure.special
        contract = built.structure.eras[special.mg_artist_id][-1]
        mg = contract.terms_json["sections"]["advances_recoupment"]["minimum_guarantee_per_period"]
        assert mg == "1200"
        # And the answer key honors the floor in every governed period.
        start = contract.effective_from
        for row in built.world.ledger:
            if row.artist_id == special.mg_artist_id and row.period >= start.isoformat()[:7]:
                assert row.net_payable >= Decimal("1200")

    def test_territory_carveout_contract(self, built: BuiltWorld) -> None:
        special = built.structure.special
        contract = built.structure.eras[special.carveout_artist_id][-1]
        doc = parse_terms_doc(contract.terms_json)
        terms = resolve_terms(doc, [], as_of=date(2026, 6, 30))
        assert "JP" in terms.excluded_territories

    def test_terminated_contract_still_accounts_post_term(self, built: BuiltWorld) -> None:
        special = built.structure.special
        contract = built.structure.eras[special.terminated_artist_id][-1]
        assert contract.effective_to == date(2026, 1, 31)
        post_term = [
            row
            for row in built.world.ledger
            if row.artist_id == special.terminated_artist_id and row.period >= "2026-02"
        ]
        assert len(post_term) == 5  # 2026-02 .. 2026-06 rows exist (may be zero-value)

    def test_exactly_one_canary_contract(self, built: BuiltWorld) -> None:
        canaries = [c for c in built.world.contracts if c.has_canary]
        assert len(canaries) == 1
        text = document_text(
            contract_document(canaries[0], built.structure, built.structure.config)
        )
        assert "SYSTEM OVERRIDE" in text
        # The canary lives in the rendered corpus only — never in canonical terms.
        assert "SYSTEM OVERRIDE" not in json.dumps(canaries[0].terms_json)

    def test_amendments_supersede_inside_the_window(self, built: BuiltWorld) -> None:
        in_window = [
            c
            for c in built.world.contracts
            if c.kind == "amendment"
            and "2025-08-01" <= c.effective_from.isoformat() <= "2026-05-31"
        ]
        assert len(in_window) >= 20  # plenty of mid-year rate changes for evals


class TestGolden:
    def test_world_fingerprint_matches_committed_golden(
        self, built: BuiltWorld, world_config: WorldConfig
    ) -> None:
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        fx = fx_rows(world_config.fx_rates, world_config.periods)
        tables = fingerprint_from_world(built.world, fx)
        assert tables == golden["tables"], (
            "world content drifted from the committed golden — if intentional, "
            "regenerate tests/golden/world_fingerprint.json and call it out in the PR"
        )
        # The combined hash recomputes from these tables + the committed file hashes.
        assert combined_hash(tables, golden["files"]) == golden["combined"]

    def test_golden_seed_is_the_default(self, built: BuiltWorld) -> None:
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        assert golden["seed"] == 20260805 == built.world.seed
