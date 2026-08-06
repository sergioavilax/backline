"""Parser-level read-only SQL policy for the ``sql_query`` tool (BUILD_PLAN §4.3).

This is the enforcement point for invariant 3 (*agents can never read the answer key*):
every query is parsed with sqlglot before it goes anywhere near Postgres, and anything
that is not exactly one SELECT (or set-operation of SELECTs) over the ``label`` /
``staging`` schemas is rejected. ``truth`` and ``app`` are not special-cased — the
allowlist is default-deny, so an unknown schema is exactly as dead as the answer key.

Also enforced here: no DML/DDL, no multi-statements, no ``SELECT INTO`` / ``FOR
UPDATE``, no side-effectful or filesystem functions, every table schema-qualified
(CTE names excepted — the parser resolves them), and the ``LIMIT`` policy (inject
``LIMIT 200`` when absent; cap larger literals). The runtime's `EXPLAIN` cost ceiling
lives in the tool handler — it needs a database; this module is pure parsing.

``sql_policy_check`` adapts the policy to the guardrails ``ToolCheck`` hook, so a
denied query is an ``Incident`` → a ``guardrail`` span in the trace, not a buried log
line (§4.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from backline.core.guardrails import Incident

ALLOWED_SCHEMAS = frozenset({"label", "staging"})
"""Readable schemas. Default-deny: truth/app/rag/pg_catalog/... are out by construction."""

DEFAULT_ROW_LIMIT = 200

# Function names (lowercased) that sleep, touch the filesystem, signal backends, mutate
# sequences/settings, or open remote connections. Matched against every function call
# anywhere in the statement, including table-function sources in FROM.
_DENIED_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_stat_file",
        "set_config",
        "nextval",
        "setval",
        "currval",
        "lastval",
        "lo_import",
        "lo_export",
        "pg_notify",
        "pg_logical_emit_message",
        "query_to_xml",
        "xpath_table",
    }
)
_DENIED_FUNCTION_PREFIXES = ("pg_read_", "pg_ls_", "dblink", "pg_advisory_")


class SqlPolicyViolation(Exception):
    """The query is outside the tool's contract; the message says exactly why."""


@dataclass(frozen=True)
class PolicyResult:
    """The vetted, possibly rewritten query the tool is allowed to execute."""

    sql: str
    limit_injected: bool
    limit_capped: bool


def _parse_single_statement(query: str) -> exp.Expr:
    if not query or not query.strip():
        raise SqlPolicyViolation("empty query")
    try:
        statements: list[exp.Expr] = [
            s for s in sqlglot.parse(query, read="postgres") if s is not None
        ]
    except sqlglot.errors.ParseError as error:
        first = error.errors[0]["description"] if error.errors else str(error)
        raise SqlPolicyViolation(f"query does not parse as PostgreSQL: {first}") from error
    if len(statements) != 1:
        raise SqlPolicyViolation(f"exactly one statement per call, got {len(statements)}")
    return statements[0]


def _check_statement_kind(statement: exp.Expr) -> exp.Query:
    if not isinstance(statement, exp.Select | exp.SetOperation):
        raise SqlPolicyViolation(
            f"only a single SELECT (or UNION/INTERSECT/EXCEPT of SELECTs) is allowed, "
            f"got {type(statement).__name__.upper()}"
        )
    for select in statement.find_all(exp.Select):
        if select.args.get("into") is not None:
            raise SqlPolicyViolation("SELECT INTO is a write — not allowed")
        if select.args.get("locks"):
            raise SqlPolicyViolation("locking clauses (FOR UPDATE/SHARE) are not allowed")
    return statement


def _check_tables(statement: exp.Query) -> None:
    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue  # table-function source (unnest(...) etc.); the function scan covers it
        if table.catalog:
            raise SqlPolicyViolation(
                f"three-part table names are not allowed: {table.catalog}.{table.db}.{table.name}"
            )
        schema = table.db
        if not schema:
            if table.name in cte_names:
                continue
            raise SqlPolicyViolation(
                f"table {table.name!r} must be schema-qualified "
                f"(readable schemas: {', '.join(sorted(ALLOWED_SCHEMAS))})"
            )
        if schema.lower() not in ALLOWED_SCHEMAS:
            raise SqlPolicyViolation(
                f"schema {schema.lower()!r} is not readable by this tool "
                f"(readable schemas: {', '.join(sorted(ALLOWED_SCHEMAS))})"
            )


def _check_functions(statement: exp.Query) -> None:
    for func in statement.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in _DENIED_FUNCTIONS or name.startswith(_DENIED_FUNCTION_PREFIXES):
            raise SqlPolicyViolation(f"function {name}() is not allowed in queries")


def _apply_limit(statement: exp.Query, row_limit: int) -> tuple[exp.Query, bool, bool]:
    existing = statement.args.get("limit")
    if existing is None:
        return statement.limit(row_limit), True, False
    value = existing.expression
    if not (isinstance(value, exp.Literal) and not value.is_string):
        raise SqlPolicyViolation("LIMIT must be a plain integer literal")
    try:
        requested = int(value.this)
    except ValueError as error:
        raise SqlPolicyViolation("LIMIT must be a plain integer literal") from error
    if requested > row_limit:
        value.replace(exp.Literal.number(row_limit))
        return statement, False, True
    return statement, False, False


def enforce(query: str, *, row_limit: int = DEFAULT_ROW_LIMIT) -> PolicyResult:
    """Vet one query; return the rewritten SQL or raise :class:`SqlPolicyViolation`."""
    parsed = _parse_single_statement(query)
    statement = _check_statement_kind(parsed)
    _check_tables(statement)
    _check_functions(statement)
    statement, injected, capped = _apply_limit(statement, row_limit)
    return PolicyResult(
        sql=statement.sql(dialect="postgres"),
        limit_injected=injected,
        limit_capped=capped,
    )


def sql_policy_check(tool_name: str, raw_args: dict[str, Any]) -> Incident | None:
    """Guardrails ``ToolCheck``: a policy violation becomes a traced incident.

    Missing or mistyped ``query`` args are left to Pydantic validation — this check
    only judges queries that exist.
    """
    if tool_name != "sql_query":
        return None
    query = raw_args.get("query")
    if not isinstance(query, str):
        return None
    try:
        enforce(query)
    except SqlPolicyViolation as violation:
        return Incident(kind="sql_policy", detail=str(violation), tool="sql_query")
    return None
