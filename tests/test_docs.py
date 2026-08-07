"""Doc-pinning tests (Phase 8): the README's claims must match the code.

The README gates first impressions, so its claims are tested like any other
interface: every relative link (and in-page anchor) must resolve, and every
number it states — tool list, agent list, suite size, world scale, results
tables — must equal what the code and the committed artifacts actually say.
A README edit that drifts from reality fails CI here.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from backline.agents import configs as agent_configs

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
PHASE_LOG = (ROOT / "docs" / "PHASE_LOG.md").read_text(encoding="utf-8")

# ── markdown helpers ─────────────────────────────────────────────────────────

# Every inline link/image target: the (...) after ](, tolerating titles omitted.
_LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def _link_targets(markdown: str) -> list[str]:
    return _LINK_TARGET.findall(markdown)


def _slugify(heading: str) -> str:
    """GitHub's anchor slug: drop non-word chars (keep spaces/hyphens), spaces → hyphens."""
    cleaned = re.sub(r"[^\w\- ]", "", heading.lower())
    return cleaned.replace(" ", "-")


def _anchor_slugs(markdown: str) -> set[str]:
    """All heading anchors GitHub generates for a document (duplicates get -N suffixes)."""
    slugs: set[str] = set()
    seen: Counter[str] = Counter()
    for match in _HEADING.finditer(markdown):
        base = _slugify(match.group(2))
        n = seen[base]
        seen[base] += 1
        slugs.add(base if n == 0 else f"{base}-{n}")
    return slugs


def _assert_links_resolve(markdown: str, base_dir: Path) -> None:
    for target in _link_targets(markdown):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, anchor = target.partition("#")
        resolved = (base_dir / path_part).resolve() if path_part else None
        if resolved is not None:
            assert resolved.exists(), f"broken link target: {target}"
        if anchor and path_part.endswith(".md"):
            assert resolved is not None
            slugs = _anchor_slugs(resolved.read_text(encoding="utf-8"))
            assert anchor in slugs, f"broken anchor: {target}"


# ── links ────────────────────────────────────────────────────────────────────


def test_readme_links_and_anchors_resolve() -> None:
    _assert_links_resolve(README, ROOT)


def test_architecture_links_and_anchors_resolve() -> None:
    _assert_links_resolve(ARCHITECTURE, ROOT / "docs")


# ── tool list / agent list ───────────────────────────────────────────────────


def _tool_names_in_code() -> set[str]:
    names: set[str] = set()
    for builders in agent_configs._TOOL_SETS.values():
        for builder in builders:
            name = builder.__name__
            assert name.startswith("build_") and name.endswith("_tool"), name
            names.add(name[len("build_") : -len("_tool")])
    return names


def test_readme_tool_list_matches_code() -> None:
    tools = _tool_names_in_code()
    bullet = re.search(r"\*\*(\d+) typed tools\*\*(.*?)(?=\n- \*\*)", README, re.DOTALL)
    assert bullet is not None, "README must state the typed-tool count"
    assert int(bullet.group(1)) == len(tools), "README tool count drifted from the code"
    listed = set(re.findall(r"`([a-z_]+)`", bullet.group(2)))
    assert listed == tools, f"README tool list drifted: {sorted(listed ^ tools)}"
    missing_in_arch = {tool for tool in tools if f"`{tool}`" not in ARCHITECTURE}
    assert not missing_in_arch, f"ARCHITECTURE.md missing tools: {sorted(missing_in_arch)}"


def test_readme_agent_list_matches_code() -> None:
    assert set(agent_configs.AGENT_NAMES) == {"counsel", "analyst", "reconciler"}
    assert "Three agents" in README
    for agent in agent_configs.AGENT_NAMES:
        assert re.search(agent, README, re.IGNORECASE), f"README never names agent {agent!r}"


# ── suite size ───────────────────────────────────────────────────────────────


def _suite() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ROOT / "evals" / "suites" / "core.json").read_text(encoding="utf-8")
    )
    return data


def test_readme_suite_size_matches_committed_suite() -> None:
    suite = _suite()
    n_questions = len(suite["questions"])
    n_categories = len(suite["counts"])
    assert sum(suite["counts"].values()) == n_questions
    hero = re.search(r"a (\d+)-question, three-tier eval suite", README)
    assert hero is not None and int(hero.group(1)) == n_questions
    results = re.search(r"\((\d+) questions, (\d+)\s+categories", README)
    assert results is not None, "README results section must state suite size"
    assert int(results.group(1)) == n_questions
    assert int(results.group(2)) == n_categories


# ── world scale ──────────────────────────────────────────────────────────────


def _fingerprint_files() -> list[str]:
    data: dict[str, Any] = json.loads(
        (ROOT / "tests" / "golden" / "world_fingerprint.json").read_text(encoding="utf-8")
    )
    return list(data["files"])


def test_readme_contract_counts_match_golden_fingerprint() -> None:
    pdfs = [f for f in _fingerprint_files() if f.endswith(".pdf")]
    base = [f for f in pdfs if "FBR-C-" in f]
    amendments = [f for f in pdfs if "FBR-A-" in f]
    assert len(base) + len(amendments) == len(pdfs)
    claim = re.search(r"(\d+)\s+contracts \((\d+) base \+ (\d+) amendments\)", README)
    assert claim is not None, "README must state the contract counts"
    assert int(claim.group(1)) == len(pdfs)
    assert int(claim.group(2)) == len(base)
    assert int(claim.group(3)) == len(amendments)
    assert f"{len(pdfs)} contracts" in README  # quickstart repeats the total


def test_readme_world_scale_numbers_have_phase_log_provenance() -> None:
    # Row/entity counts aren't derivable from committed artifacts without a seeded
    # DB, so the README may only claim numbers the build record (PHASE_LOG) pins.
    for claim in ("468,160 statement lines", "150 artists", "549 releases", "2,366 tracks"):
        assert claim in README, f"README dropped the world-scale claim {claim!r}"
        assert claim.split(" ")[0] in PHASE_LOG, f"PHASE_LOG lacks provenance for {claim!r}"


def test_readme_corpus_math_is_consistent_and_provenanced() -> None:
    claim = re.search(r"([\d.]+)M (?:corpus )?tokens[^.]*?([\d.]+)\u00d7 a (\d+)K", README)
    assert claim is not None, "README must state the corpus-token multiple"
    tokens_m, multiple, window_k = claim.groups()
    computed = float(tokens_m) * 1_000_000 / (float(window_k) * 1_000)
    assert abs(computed - float(multiple)) < 0.05, "corpus multiple doesn't match its own math"
    # The exact o200k count is operator-measured (tiktoken needs network); the
    # build record must carry the same figure the README claims.
    assert f"{tokens_m}M" in PHASE_LOG, "PHASE_LOG lacks provenance for the corpus token count"


# ── results tables ───────────────────────────────────────────────────────────


def _table_row(markdown: str, first_cell: str) -> list[str]:
    for line in markdown.splitlines():
        if line.startswith(f"| {first_cell} |"):
            return [cell.strip() for cell in line.strip().strip("|").split("|")]
    raise AssertionError(f"README table row {first_cell!r} not found")


def _fmt1(value: float) -> str:
    return f"{value:.1f}"


def test_readme_sweep_table_matches_committed_results() -> None:
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        doc: dict[str, Any] = json.loads(
            (ROOT / "benchmarks" / "results" / f"{model}.json").read_text(encoding="utf-8")
        )
        row = _table_row(README, model)
        assert row[1] == _fmt1(doc["overall_score"]), f"{model} overall drifted"
        assert row[2] == f"${doc['usd_per_query']}", f"{model} $/query drifted"
        assert row[3] == f"{doc['latency_ms_p50'] / 1000:.1f}s", f"{model} p50 drifted"
        assert row[4] == f"{doc['latency_ms_p95'] / 1000:.1f}s", f"{model} p95 drifted"
        assert row[5] == _fmt1(doc["iterations_mean"]), f"{model} iterations drifted"
        assert row[6] == f"{doc['tool_calls']['error_rate'] * 100:.1f}%", f"{model} errors drifted"
        assert row[7] == f"{doc['runs']['exhausted']}/{doc['n_scored']}", f"{model} exhausted"
        spend = Decimal(doc["total_cost_usd"]).quantize(Decimal("0.01"))
        assert row[8] == f"${spend}", f"{model} run spend drifted"


def _live_baseline() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ROOT / "evals" / "results" / "baseline.json").read_text(encoding="utf-8")
    )
    entries: list[dict[str, Any]] = data["baselines"]
    for entry in entries:
        if entry["track"] == "platform" and entry["subset"] == "full":
            return entry
    raise AssertionError("no live (platform, full) baseline entry committed")


def test_readme_baseline_table_matches_committed_baseline() -> None:
    categories: dict[str, float] = _live_baseline()["categories"]
    perfect = [c for c in categories if categories[c] == 100.0]
    merged_row = _table_row(README, ", ".join(sorted(perfect, key=_suite_category_order)))
    assert merged_row[1] == "100.0"
    for category in ("reconciliation", "adversarial", "contract_terms", "multi_step"):
        assert _table_row(README, category)[1] == _fmt1(categories[category]), category


def _suite_category_order(category: str) -> int:
    order = (
        "catalog_lookup",
        "contract_terms",
        "royalty_math",
        "recoupment_state",
        "cross_collateral",
        "sql_analytics",
        "reconciliation",
        "multi_step",
        "abstention",
        "adversarial",
    )
    return order.index(category)


def test_readme_baseline_overall_is_the_weighted_mean() -> None:
    entry = _live_baseline()
    counts: dict[str, int] = _suite()["counts"]
    categories: dict[str, float] = entry["categories"]
    assert set(counts) == set(categories)
    weighted = sum(categories[c] * n for c, n in counts.items()) / sum(counts.values())
    claim = re.search(rf"weighted overall ({_fmt1(weighted)})", README)
    assert claim is not None, f"README must claim the weighted overall {_fmt1(weighted)}"
    assert entry["model"] in README


def test_readme_same_model_variance_pair_matches_artifacts() -> None:
    # The sweep-table footnote and the Limits bullet both quantify the sonnet
    # composite-vs-fresh pair; composite, fresh, and their spread must
    # recompute from the committed artifacts (D-033: derivable claims
    # drift-fail — a moved baseline may not leave a stale variance story).
    counts: dict[str, int] = _suite()["counts"]
    categories: dict[str, float] = _live_baseline()["categories"]
    composite = sum(categories[c] * n for c, n in counts.items()) / sum(counts.values())
    fresh: dict[str, Any] = json.loads(
        (ROOT / "benchmarks" / "results" / "claude-sonnet-5.json").read_text(encoding="utf-8")
    )
    pair = f"{_fmt1(composite)} composite vs {_fmt1(fresh['overall_score'])} fresh"
    assert README.count(pair) >= 2, f"footnote and Limits must state the pair as {pair!r}"
    spread = Decimal(_fmt1(composite)) - Decimal(_fmt1(fresh["overall_score"]))
    assert f"{spread} overall points" in README, "Limits must quantify the same-model spread"


def test_readme_retrieval_table_matches_phase_log_probe() -> None:
    # The probe numbers are a recorded live measurement (PHASE_LOG, real-model
    # run); the README table must repeat them exactly, mode for mode.
    real_block = PHASE_LOG.split("Real-model retrieval probe (manual", 1)[1]
    probe: dict[str, tuple[str, str]] = {}
    for line in real_block.splitlines()[:20]:
        cells = line.split()
        if cells and cells[0] in {
            "scoped/fused",
            "scoped/rerank",
            "unscoped/fused",
            "unscoped/rerank",
        }:
            probe[cells[0]] = (cells[1], cells[5])  # MRR, R@10
    assert len(probe) == 4, "PHASE_LOG probe table not parseable"
    readme_modes = {
        "governing-scoped, fused": "scoped/fused",
        "governing-scoped, reranked": "scoped/rerank",
        "unscoped, fused": "unscoped/fused",
        "unscoped, reranked": "unscoped/rerank",
    }
    for readme_label, probe_mode in readme_modes.items():
        row = [cell.replace("*", "") for cell in _table_row(README, readme_label)]
        assert row[1] == probe[probe_mode][0], f"{readme_label}: MRR drifted"
        assert row[2] == probe[probe_mode][1], f"{readme_label}: R@10 drifted"


def test_readme_states_pending_local_row() -> None:
    # Degrade-gracefully (BUILD_PLAN §7): until local-qwen.json lands, the README
    # presents API rows and names the pending row with its procedure.
    if (ROOT / "benchmarks" / "results" / "local-qwen.json").exists():
        return  # row landed; the pending note may legitimately be gone
    assert "local-qwen" in README and "pending" in README
    assert "benchmarks/LOCAL.md" in README


# ── commands, API surface, license ───────────────────────────────────────────


def test_readme_make_targets_exist() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, re.MULTILINE))
    # Only commands the README shows as code count as claims (prose like
    # "headings make clause chunking easy" must not trip this).
    code = "\n".join(re.findall(r"```.*?```", README, re.DOTALL) + re.findall(r"`[^`\n]+`", README))
    referenced = set(re.findall(r"make ([a-z][a-z-]*)", code))
    assert referenced, "README shows no make commands?"
    unknown = referenced - targets
    assert not unknown, f"README references make targets that don't exist: {sorted(unknown)}"


def test_architecture_api_path_count_matches_openapi() -> None:
    claim = re.search(r"FastAPI \((\d+) paths", ARCHITECTURE)
    assert claim is not None, "ARCHITECTURE.md must state the API path count"
    openapi: dict[str, Any] = json.loads(
        (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
    )
    assert int(claim.group(1)) == len(openapi["paths"])


def test_license_is_mit_and_linked() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = { text = "MIT" }' in pyproject
    assert "](LICENSE)" in README
