"""Adversarial matrix for the parser-level SQL policy (BUILD_PLAN §4.3, invariant 3).

The policy is the only thing between an agent and the answer key, so these tests are
deliberately hostile: schema smuggling via casing/quoting/joins/subqueries/set-ops,
DML/DDL, multi-statements, side-effectful functions, LIMIT games. Keyless — the policy
is pure parsing, no database involved.
"""

import pytest

from backline.tools.sqlpolicy import (
    ALLOWED_SCHEMAS,
    SqlPolicyViolation,
    enforce,
    sql_policy_check,
)


def denied(query: str) -> str:
    """Assert the query is rejected; return the violation message."""
    with pytest.raises(SqlPolicyViolation) as excinfo:
        enforce(query)
    return str(excinfo.value)


# ── allowed shapes ───────────────────────────────────────────────────────────


def test_plain_select_from_label_allowed() -> None:
    result = enforce("SELECT id, stage_name FROM label.artists WHERE id = 7")
    assert "label.artists" in result.sql


def test_staging_is_readable() -> None:
    result = enforce("SELECT status, period FROM staging.statement_batches")
    assert result.limit_injected


def test_cte_allowed_and_cte_names_need_no_schema() -> None:
    result = enforce(
        """
        WITH top_tracks AS (
            SELECT isrc, SUM(gross_amount) AS total
            FROM label.statement_lines GROUP BY isrc
        )
        SELECT t.title, tt.total
        FROM top_tracks tt JOIN label.tracks t ON t.isrc = tt.isrc
        ORDER BY tt.total DESC
        """
    )
    assert "top_tracks" in result.sql


def test_union_of_allowed_schemas_allowed() -> None:
    enforce(
        "SELECT isrc FROM label.tracks UNION ALL SELECT isrc FROM label.statement_lines LIMIT 5"
    )


def test_join_and_subquery_within_label_allowed() -> None:
    enforce(
        """
        SELECT a.stage_name,
               (SELECT COUNT(*) FROM label.tracks t WHERE t.primary_artist_id = a.id) AS n
        FROM label.artists a
        WHERE a.id IN (SELECT primary_artist_id FROM label.tracks)
        """
    )


def test_aggregate_without_limit_gets_limit() -> None:
    result = enforce("SELECT COUNT(*) FROM label.statement_lines")
    assert result.limit_injected
    assert "LIMIT 200" in result.sql


# ── the answer key stays sealed (invariant 3) ────────────────────────────────


def test_truth_schema_is_rejected() -> None:
    message = denied("SELECT * FROM truth.expected_ledger")
    assert "truth" in message


def test_truth_schema_stays_excluded_from_allowlist() -> None:
    # The invariant-3 canary: if someone ever "helpfully" allowlists truth or app,
    # this fails before any eval can leak the answer key.
    assert "truth" not in ALLOWED_SCHEMAS
    assert "app" not in ALLOWED_SCHEMAS
    assert frozenset({"label", "staging"}) == ALLOWED_SCHEMAS


def test_truth_rejected_in_join() -> None:
    denied(
        "SELECT a.stage_name, e.net_payable FROM label.artists a "
        "JOIN truth.expected_ledger e ON e.artist_id = a.id"
    )


def test_truth_rejected_in_subquery() -> None:
    denied(
        "SELECT * FROM label.artists WHERE id IN "
        "(SELECT artist_id FROM truth.expected_ledger WHERE net_payable > 0)"
    )


def test_truth_rejected_in_cte_body() -> None:
    denied("WITH t AS (SELECT * FROM truth.anomaly_registry) SELECT * FROM t")


def test_truth_rejected_in_union_arm() -> None:
    denied("SELECT id FROM label.artists UNION SELECT artist_id FROM truth.expected_ledger")


def test_truth_rejected_regardless_of_casing_and_quoting() -> None:
    denied('SELECT * FROM "truth"."expected_ledger"')
    denied("SELECT * FROM TRUTH.EXPECTED_LEDGER")
    denied("SELECT * FROM tRuTh.expected_ledger")
    denied('SELECT * FROM "TRUTH".expected_ledger')


def test_app_and_rag_and_catalogs_rejected() -> None:
    denied("SELECT * FROM app.runs")
    denied("SELECT * FROM app.spans")
    denied("SELECT * FROM rag.contract_chunks")
    denied("SELECT * FROM pg_catalog.pg_tables")
    denied("SELECT * FROM information_schema.tables")


def test_unqualified_table_rejected() -> None:
    message = denied("SELECT * FROM artists")
    assert "schema" in message.lower()


def test_three_part_names_rejected() -> None:
    denied("SELECT * FROM otherdb.truth.expected_ledger")


def test_cte_cannot_launder_a_forbidden_schema() -> None:
    # A CTE *named* truth is fine as a name, but its body must still be legal...
    denied("WITH truth AS (SELECT * FROM truth.expected_ledger) SELECT * FROM truth")
    # ...and a legal body under a suggestive name stays legal.
    enforce("WITH truth AS (SELECT id FROM label.artists) SELECT * FROM truth")


# ── read-only: DML/DDL and friends ───────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO staging.flags (batch_id, kind, severity) VALUES (1, 'x', 'low')",
        "UPDATE label.artists SET stage_name = 'pwned' WHERE id = 1",
        "DELETE FROM label.statement_lines WHERE id = 1",
        "TRUNCATE label.statement_lines",
        "DROP TABLE truth.expected_ledger",
        "CREATE TABLE label.evil (id int)",
        "ALTER TABLE label.artists ADD COLUMN x int",
        "GRANT SELECT ON truth.expected_ledger TO PUBLIC",
        "MERGE INTO label.artists a USING label.tracks t ON a.id = t.id "
        "WHEN MATCHED THEN UPDATE SET stage_name = 'x'",
        "COPY label.artists TO '/tmp/out.csv'",
        "VALUES (1, 2)",
        "SET search_path TO truth",
        "SHOW search_path",
        "EXPLAIN SELECT * FROM label.artists",
    ],
)
def test_non_select_statements_rejected(query: str) -> None:
    denied(query)


def test_multiple_statements_rejected() -> None:
    message = denied("SELECT id FROM label.artists; SELECT 1")
    assert "one statement" in message


def test_empty_and_garbage_rejected() -> None:
    denied("")
    denied("   ;  ")
    denied("SELECT FROM WHERE")
    denied("not sql at all ~~~")


def test_select_into_rejected() -> None:
    denied("SELECT * INTO label.copycat FROM label.artists")


def test_locking_clauses_rejected() -> None:
    denied("SELECT * FROM label.artists FOR UPDATE")
    denied("SELECT * FROM label.artists FOR SHARE")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_sleep(30)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('.') ",
        "SELECT pg_terminate_backend(1)",
        "SELECT set_config('search_path', 'truth', false)",
        "SELECT nextval('staging.statement_batches_id_seq')",
        "SELECT setval('staging.statement_batches_id_seq', 99)",
        "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int)",
        "SELECT pg_advisory_lock(1)",
        "SELECT lo_import('/etc/passwd')",
    ],
)
def test_side_effectful_functions_rejected(query: str) -> None:
    denied(query)


# ── LIMIT policy ─────────────────────────────────────────────────────────────


def test_limit_injected_when_absent() -> None:
    result = enforce("SELECT id FROM label.artists")
    assert result.limit_injected and not result.limit_capped
    assert "LIMIT 200" in result.sql


def test_existing_small_limit_kept() -> None:
    result = enforce("SELECT id FROM label.artists LIMIT 10")
    assert not result.limit_injected and not result.limit_capped
    assert "LIMIT 10" in result.sql


def test_oversize_limit_capped() -> None:
    result = enforce("SELECT id FROM label.statement_lines LIMIT 100000")
    assert result.limit_capped
    assert "LIMIT 200" in result.sql
    assert "100000" not in result.sql


def test_inner_limit_untouched_outer_injected() -> None:
    result = enforce("SELECT * FROM (SELECT id FROM label.statement_lines LIMIT 5000) AS sub")
    assert result.limit_injected
    assert "LIMIT 5000" in result.sql  # the subquery keeps its own limit
    assert result.sql.rstrip().endswith("LIMIT 200")


def test_union_gets_one_outer_limit() -> None:
    result = enforce("SELECT id FROM label.artists UNION SELECT id FROM label.releases")
    assert result.limit_injected
    assert result.sql.rstrip().endswith("LIMIT 200")


def test_non_literal_limit_rejected() -> None:
    denied("SELECT id FROM label.artists LIMIT (SELECT COUNT(*) FROM label.artists)")


def test_custom_row_limit_respected() -> None:
    result = enforce("SELECT id FROM label.artists", row_limit=50)
    assert "LIMIT 50" in result.sql


# ── the guardrails ToolCheck ─────────────────────────────────────────────────


def test_tool_check_flags_forbidden_query_as_incident() -> None:
    incident = sql_policy_check("sql_query", {"query": "SELECT * FROM truth.expected_ledger"})
    assert incident is not None
    assert incident.kind == "sql_policy"
    assert incident.tool == "sql_query"
    assert "truth" in incident.detail


def test_tool_check_passes_clean_query() -> None:
    assert sql_policy_check("sql_query", {"query": "SELECT id FROM label.artists"}) is None


def test_tool_check_ignores_other_tools() -> None:
    assert sql_policy_check("search_contracts", {"query": "DROP TABLE x"}) is None


def test_tool_check_tolerates_missing_query_arg() -> None:
    # Missing/mistyped args are Pydantic-validation territory, not policy territory.
    assert sql_policy_check("sql_query", {}) is None
