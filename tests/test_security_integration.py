"""Adversarial integration tests against a LIVE PostgreSQL (docker compose, e-commerce DB).

Three independent layers, each tested on its own so that a regression in one cannot hide
behind another:

  1. AST gate      (validator.py)  -- the SQL text is refused before the database sees it
  2. EXPLAIN gate  (plan_gate.py)  -- the planner's own plan is refused before execution
  3. The database  (db/init_roles.sql) -- bypass layers 1 and 2 entirely and connect as
                                         the application role: the server still says no

Run:  docker compose up -d --wait && python db/seed_ecommerce.py && pytest tests/test_security_integration.py
Without the database the tests skip locally and FAIL when CI=true (a green CI that silently
skipped its security suite would be worse than no suite).
"""

from __future__ import annotations

import json
import logging
import os
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from nl2sql import api
from nl2sql.config import Settings
from nl2sql.db import fetch_all, readonly_cursor
from nl2sql.linker import link_schema
from nl2sql.llm import LLMResponse
from nl2sql.pipeline import Pipeline
from nl2sql.plan_gate import Verdict, check_plan
from nl2sql.schema import introspect
from nl2sql.telemetry import LOGGER_NAME
from nl2sql.validator import Code, validate

ADMIN = os.environ.get(
    "NL2SQL_ADMIN_DSN", "postgresql://nl2sql:nl2sql_admin_dev@localhost:5433/nl2sql_shop"
)
RO = os.environ.get(
    "NL2SQL_READONLY_DSN", "postgresql://nl2sql_ro:nl2sql_ro_dev@localhost:5433/nl2sql_shop"
)


@pytest.fixture(scope="module")
def settings() -> Settings:
    cfg = Settings(
        NL2SQL_ADMIN_DSN=ADMIN, NL2SQL_READONLY_DSN=RO, NL2SQL_DOMAIN="ecommerce",
        NL2SQL_STATEMENT_TIMEOUT_MS=3000, NL2SQL_LARGE_TABLE_ROWS=20000,
        NL2SQL_MAX_PLAN_COST=100000, NL2SQL_FEW_SHOT_COUNT=0,
    )
    try:
        with psycopg.connect(cfg.readonly_dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as err:
        msg = f"e-commerce database not reachable ({err}); run `docker compose up -d --wait`"
        if os.environ.get("CI"):
            pytest.fail(msg)
        pytest.skip(msg)
    return cfg


@pytest.fixture(scope="module")
def catalog(settings: Settings):
    cat = introspect(settings)
    assert len(cat.tables) == 7
    return cat


def codes(sql: str, catalog, settings: Settings) -> set[Code]:
    return {i.code for i in validate(sql, catalog, settings).errors}


# --------------------------------------------------------------------------- layer 1: AST


@pytest.mark.parametrize(
    ("label", "sql", "expected"),
    [
        ("stacked queries", "SELECT id FROM customers LIMIT 1; DROP TABLE orders", Code.MULTIPLE_STATEMENTS),
        ("stacked with comment", "SELECT 1; /* x */ DELETE FROM payments", Code.MULTIPLE_STATEMENTS),
        ("DELETE", "DELETE FROM payments", Code.NOT_A_READ),
        ("UPDATE", "UPDATE customers SET email = 'x@example.com'", Code.NOT_A_READ),
        ("INSERT", "INSERT INTO categories (id, name) VALUES (999, 'x')", Code.NOT_A_READ),
        ("DROP TABLE", "DROP TABLE orders", Code.NOT_A_READ),
        ("TRUNCATE", "TRUNCATE payments", Code.NOT_A_READ),
        ("CREATE TABLE", "CREATE TABLE evil (id int)", Code.NOT_A_READ),
        ("data-modifying CTE",
         "WITH d AS (DELETE FROM payments RETURNING *) SELECT * FROM d LIMIT 5", Code.WRITE_IN_SUBTREE),
        ("pg_sleep", "SELECT pg_sleep(30)", Code.FORBIDDEN_FUNCTION),
        ("pg_read_file", "SELECT pg_read_file('/etc/passwd')", Code.FORBIDDEN_FUNCTION),
        ("dblink", "SELECT * FROM dblink('host=evil', 'select 1') AS t(x int) LIMIT 1", Code.FORBIDDEN_FUNCTION),
        ("unlisted pg_ function (prefix rule)", "SELECT pg_ls_waldir() LIMIT 1", Code.FORBIDDEN_FUNCTION),
        ("current_setting", "SELECT current_setting('data_directory')", Code.FORBIDDEN_FUNCTION),
        ("information_schema", "SELECT table_name FROM information_schema.tables LIMIT 5", Code.SYSTEM_CATALOG),
        ("pg_catalog qualified", "SELECT relname FROM pg_catalog.pg_class LIMIT 5", Code.SYSTEM_CATALOG),
        ("pg_shadow unqualified", "SELECT usename, passwd FROM pg_shadow LIMIT 5", Code.SYSTEM_CATALOG),
        ("CROSS JOIN", "SELECT o.id FROM orders o CROSS JOIN order_items i LIMIT 10", Code.CROSS_JOIN),
        ("comma join w/o condition", "SELECT o.id FROM orders o, reviews r LIMIT 10", Code.CROSS_JOIN),
        ("JOIN ON true", "SELECT o.id FROM orders o JOIN products p ON true LIMIT 10", Code.CROSS_JOIN),
        ("SELECT INTO", "SELECT * INTO evil FROM customers", Code.SELECT_INTO),
        ("FOR UPDATE lock", "SELECT id FROM orders LIMIT 5 FOR UPDATE", Code.LOCKING_CLAUSE),
        ("foreign schema", "SELECT * FROM secret.orders LIMIT 5", Code.UNKNOWN_TABLE),
    ],
)
def test_ast_gate_rejects(label: str, sql: str, expected: Code, catalog, settings: Settings) -> None:
    assert expected in codes(sql, catalog, settings), label


def test_ast_gate_allows_bounded_and_legitimate_shapes(catalog, settings: Settings) -> None:
    for sql in [
        "SELECT id FROM customers ORDER BY id LIMIT 5",
        "SELECT count(*) FROM orders",  # scalar aggregate needs no LIMIT
        "SELECT c.id FROM customers c JOIN orders o ON o.customer_id = c.id LIMIT 5",
        "SELECT o.id FROM orders o CROSS JOIN (SELECT count(*) AS n FROM orders) t LIMIT 5",
        "SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id LIMIT 5",
        "SELECT id FROM customers UNION ALL SELECT id FROM products LIMIT 10",
    ]:
        assert validate(sql, catalog, settings).ok, sql


def test_limit_is_injected_on_non_scalar_queries_only(catalog, settings: Settings) -> None:
    pipe = Pipeline(provider=Scripted(["SELECT id, status FROM orders ORDER BY id"]),
                    catalog=catalog, settings=settings)
    ans = pipe.answer("list orders", schema_linking=False)
    assert ans.status == "ok" and ans.sql is not None and "LIMIT 100" in ans.sql.upper()
    assert ans.row_count == 100
    scalar = Pipeline(provider=Scripted(["SELECT count(*) AS n FROM orders"]),
                      catalog=catalog, settings=settings).answer("how many", schema_linking=False)
    assert scalar.status == "ok" and "LIMIT" not in (scalar.sql or "").upper()
    assert scalar.rows == [(10_000,)]


# ------------------------------------------------------------------------- layer 2: EXPLAIN


def test_plan_gate_blocks_high_cost_plan(settings: Settings) -> None:
    # Bypasses the AST gate on purpose: a 10k x 25k Cartesian product, even with a LIMIT,
    # is priced by the planner far above the budget.
    gate = check_plan(
        "SELECT o.id, i.id FROM orders o, order_items i ORDER BY o.total_amount, i.unit_price LIMIT 10",
        settings=settings,
    )
    assert gate.verdict is Verdict.BLOCK
    assert "COST_EXCEEDED" in {f.code for f in gate.findings}
    assert gate.plan.total_cost > settings.max_plan_cost


def test_plan_gate_blocks_selective_seq_scan_on_large_table(settings: Settings) -> None:
    gate = check_plan("SELECT id FROM order_items WHERE discount_pct = 0.50 LIMIT 10", settings=settings)
    assert gate.verdict is Verdict.BLOCK
    assert "SELECTIVE_SEQ_SCAN_LARGE_TABLE" in {f.code for f in gate.findings}


def test_plan_gate_allows_indexed_lookup_and_only_warns_on_full_scan(settings: Settings) -> None:
    indexed = check_plan("SELECT id FROM order_items WHERE order_id = 42 LIMIT 10", settings=settings)
    assert indexed.verdict is Verdict.ALLOW
    full = check_plan("SELECT count(*) FROM order_items", settings=settings)
    assert full.verdict is Verdict.WARN  # reads every row, but an aggregate legitimately must


# ---------------------------------------------------------------- layer 3: the database itself


def test_role_is_configured_as_specified(settings: Settings) -> None:
    with psycopg.connect(settings.admin_dsn) as conn:
        (cfg,) = conn.execute("SELECT rolconfig FROM pg_roles WHERE rolname = 'nl2sql_ro'").fetchone()
        (is_super,) = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'nl2sql_ro'").fetchone()
    assert not is_super
    assert "default_transaction_read_only=on" in cfg
    assert "statement_timeout=3000ms" in cfg
    assert "work_mem=16MB" in cfg


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM payments",
        "UPDATE customers SET email = 'x'",
        "INSERT INTO categories (id, name) VALUES (999, 'x')",
        "DROP TABLE orders",
        "CREATE TABLE evil (id int)",
        "TRUNCATE reviews",
        "SELECT usename, passwd FROM pg_shadow",
    ],
)
def test_database_refuses_writes_and_secrets_even_if_every_gate_is_bypassed(
    statement: str, settings: Settings
) -> None:
    with psycopg.connect(settings.readonly_dsn, autocommit=True) as conn, pytest.raises(psycopg.Error):
        conn.execute(statement)


def test_role_cannot_switch_off_read_only_and_write(settings: Settings) -> None:
    """Flipping default_transaction_read_only is allowed -- and still gets nothing: the role
    simply holds no DELETE privilege (second, independent wall)."""
    with psycopg.connect(settings.readonly_dsn, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM payments")


def test_statement_timeout_cancels_a_long_query_at_the_server(settings: Settings) -> None:
    started = time.perf_counter()
    with pytest.raises(psycopg.errors.QueryCanceled), readonly_cursor(settings) as cur:
        cur.execute("SELECT pg_sleep(10)")  # not validated on purpose: DB-level defence
    elapsed = time.perf_counter() - started
    assert 2.5 < elapsed < 5.0, f"cancelled after {elapsed:.2f}s, expected ~3s"


def test_statement_timeout_applies_to_a_raw_role_connection(settings: Settings) -> None:
    """No pool, no client-side SET: the 3 s limit comes from the role itself."""
    started = time.perf_counter()
    with psycopg.connect(settings.readonly_dsn, autocommit=True) as conn, pytest.raises(
        psycopg.errors.QueryCanceled
    ):
        conn.execute("SELECT pg_sleep(10)")
    assert time.perf_counter() - started < 5.0


def test_no_attack_above_changed_any_data(settings: Settings) -> None:
    _, rows = fetch_all(
        "SELECT (SELECT count(*) FROM payments), (SELECT count(*) FROM customers), "
        "(SELECT count(*) FROM orders), (SELECT count(*) FROM categories)", settings=settings)
    assert rows == [(10_000, 2_000, 10_000, 20)]


# ----------------------------------------------------------------- pipeline + HTTP behaviour


class Scripted:
    """Provider returning a fixed sequence of SQL strings, recording how often it was asked."""

    name = "scripted"

    def __init__(self, sqls: list[str]) -> None:
        self.sqls = sqls
        self.calls = 0

    def generate(self, prompt) -> LLMResponse:
        sql = self.sqls[min(self.calls, len(self.sqls) - 1)]
        self.calls += 1
        return LLMResponse("sql", sql, None, "scripted", provider=self.name, model=self.name)


def test_security_violation_is_never_retried(catalog, settings: Settings) -> None:
    provider = Scripted(["DELETE FROM payments", "SELECT count(*) FROM payments"])
    ans = Pipeline(provider=provider, catalog=catalog, settings=settings).answer("wipe payments")
    assert ans.status == "rejected" and ans.security_violation
    assert provider.calls == 1 and ans.retries == 0  # the benign 2nd answer was never requested


def test_fixable_error_is_repaired_by_a_bounded_retry(catalog, settings: Settings) -> None:
    provider = Scripted(["SELECT bogus_col FROM customers LIMIT 3",
                         "SELECT id FROM customers ORDER BY id LIMIT 3"])
    ans = Pipeline(provider=provider, catalog=catalog, settings=settings).answer("first customers")
    assert ans.status == "ok" and ans.retries == 1 and ans.row_count == 3


def test_retry_budget_is_hard_capped_at_two(catalog, settings: Settings) -> None:
    provider = Scripted(["SELECT bogus_col FROM customers LIMIT 3"])
    ans = Pipeline(provider=provider, catalog=catalog, settings=settings).answer("first customers")
    assert ans.status == "rejected" and not ans.security_violation
    assert provider.calls == 3 and ans.retries == 2  # 1 attempt + 2 retries, never more


def test_http_403_for_security_violation_and_200_otherwise(
    catalog, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(api.app)
    monkeypatch.setattr(api, "_pipeline", Pipeline(
        provider=Scripted(["DROP TABLE orders"]), catalog=catalog, settings=settings))
    blocked = client.post("/api/ask", json={"question": "drop orders"})
    assert blocked.status_code == 403
    assert blocked.json()["security_violation"] is True and blocked.json()["retries"] == 0

    monkeypatch.setattr(api, "_pipeline", Pipeline(
        provider=Scripted(["SELECT count(*) AS n FROM customers"]), catalog=catalog, settings=settings))
    ok = client.post("/api/ask", json={"question": "how many customers"})
    assert ok.status_code == 200 and ok.json()["rows"] == [[2000]]


def test_telemetry_record_has_required_fields(
    catalog, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        Pipeline(provider=Scripted(["SELECT bogus FROM orders LIMIT 1",
                                    "SELECT count(*) AS n FROM orders"]),
                 catalog=catalog, settings=settings).answer("how many orders are there?")
    rec = json.loads(caplog.records[-1].getMessage())
    for key in ("latency_ms", "retries_used", "tables_selected", "estimated_cost", "status"):
        assert key in rec
    assert rec["retries_used"] == 1 and rec["status"] == "ok"
    assert rec["estimated_cost"] > 0 and "orders" in rec["tables_selected"]
    assert "how many orders" not in json.dumps(rec)  # question text is never logged


# ------------------------------------------------------------------------- schema linking


def test_schema_linking_picks_relevant_tables_and_fk_neighbours(catalog) -> None:
    res = link_schema("What is the average rating per product?", catalog)
    assert {"reviews", "products"} <= set(res.selected)
    assert len(res.selected) < len(catalog.tables) or res.fell_back is False
    nothing = link_schema("zzzz qqqq", catalog)
    assert nothing.fell_back and set(nothing.selected) == set(catalog.tables)


def test_introspection_captures_keys_and_sample_values(catalog) -> None:
    orders = catalog.tables["orders"]
    assert orders.primary_key == ("id",)
    assert any(fk.ref_table == "customers" for fk in orders.foreign_keys)
    seg = catalog.tables["customers"].column("segment")
    assert seg is not None and "corporate" in seg.samples
    assert catalog.tables["customers"].column("email").samples == ()  # PII never sampled
