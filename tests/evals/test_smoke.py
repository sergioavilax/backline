"""End-to-end eval-smoke tests (§5.4 DoD): the keyless plumbing run is green and the
committed mock baseline gives the regression gate real teeth on every PR.

``test_sabotaged_answer_trips_the_committed_gate`` is the gate-of-the-gate proven at
system level: one deliberately wrong scripted answer (skipping the calculator and
inventing a number) must drop its category below the committed 100 AND surface as a
T2 violation — and the gate must fail on both."""

import asyncpg
import pytest

from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from evals.gate import evaluate_gate, load_baseline
from evals.smoke import build_platform_script, run_smoke
from evals.types import load_suite
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres


@pytest.fixture(autouse=True)
async def chunks_ready(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


def test_every_smoke_question_scripts_cleanly() -> None:
    """Suite drift guard: each in_smoke question's meta still supports its script."""
    suite = load_suite("core")
    for question in suite.subset("smoke"):
        turns = build_platform_script(question)
        assert turns, question.id
        assert turns[-1].tool_calls == [], f"{question.id}: script must end in text"


async def test_smoke_green_and_gated_against_committed_baseline(
    world_env: WorldEnv, tmp_path_factory: pytest.TempPathFactory
) -> None:
    out = tmp_path_factory.mktemp("smoke-out")
    summaries, passes = await run_smoke(
        database_url=world_env.database_url,
        data_dir=world_env.data_dir,
        out_dir=out,
    )
    assert [s.track for s in summaries] == ["platform", "b0", "b1"]
    for summary in summaries:
        assert summary.n_scored == 10
        assert summary.t2_violations == 0
        for category, bucket in summary.categories.items():
            assert bucket["score"] == 100.0, (summary.track, category)
    # The committed baseline has entries for all three shapes → real PASS, not bootstrap.
    doc = load_baseline()
    for summary in summaries:
        result = evaluate_gate(summary.as_dict(), doc)
        assert result.passed and not result.bootstrap, summary.track
    assert passes == [True, True, True]
    # The platform smoke exercises the injection canary end-to-end (§5 DoD:
    # "injection suite passing"): the adversarial question scored a full T2.
    platform = summaries[0]
    assert platform.categories["adversarial"]["tiers"]["t2"] == 100.0


async def test_sabotaged_answer_trips_the_committed_gate(
    world_env: WorldEnv, tmp_path_factory: pytest.TempPathFactory
) -> None:
    out = tmp_path_factory.mktemp("smoke-sabotage")
    summaries, passes = await run_smoke(
        database_url=world_env.database_url,
        data_dir=world_env.data_dir,
        out_dir=out,
        sabotage_question_id="royalty_math-001",
    )
    platform = summaries[0]
    # The wrong answer zeroed its category and skipped the calculator (T2 violation).
    assert platform.categories["royalty_math"]["score"] == 0.0
    assert platform.t2_violations == 1
    result = evaluate_gate(platform.as_dict(), load_baseline())
    assert not result.passed
    assert any("royalty_math" in reason for reason in result.reasons)
    assert any("T2 violation" in reason for reason in result.reasons)
    assert passes[0] is False
    # The unsabotaged tracks still pass — the failure is localized and attributable.
    assert passes[1] and passes[2]
