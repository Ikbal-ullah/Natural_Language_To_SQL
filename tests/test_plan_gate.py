"""The plan gate: refusing work before paying for it."""

from __future__ import annotations

import time

from nl2sql.plan_gate import Verdict, check_plan
from tests.conftest import needs_db

HOSTILE = (
    "SELECT count(*) FROM submissions a "
    "JOIN submissions b ON a.score = b.score AND a.enrollment_id <> b.enrollment_id"
)


@needs_db
def test_cheap_query_is_allowed() -> None:
    gate = check_plan("SELECT id FROM learners WHERE id = 42 LIMIT 10")
    assert gate.verdict is Verdict.ALLOW
    assert gate.summary


@needs_db
def test_ruinous_query_is_blocked() -> None:
    gate = check_plan(HOSTILE)
    assert gate.verdict is Verdict.BLOCK
    assert any(f.code == "COST_EXCEEDED" for f in gate.findings)


@needs_db
def test_gate_plans_but_does_not_execute() -> None:
    """The whole value of the gate is that a plan is an estimate obtained for free.

    Built on EXPLAIN ANALYZE it would perform the exact work it exists to prevent; this
    query takes seconds to run and about a millisecond to plan.
    """
    started = time.perf_counter()
    gate = check_plan(HOSTILE)
    assert gate.verdict is Verdict.BLOCK
    assert time.perf_counter() - started < 1.5


@needs_db
def test_full_scan_of_a_large_table_warns_rather_than_blocks() -> None:
    """'How many submissions are there' legitimately needs every row."""
    gate = check_plan("SELECT count(*) FROM submissions WHERE runtime_ms > 1000")
    assert gate.verdict is Verdict.WARN
    assert any(f.code == "FULL_SCAN_LARGE_TABLE" for f in gate.findings)


@needs_db
def test_summary_is_written_for_a_human() -> None:
    summary = check_plan("SELECT count(*) FROM submissions WHERE runtime_ms > 1000").summary
    assert "reads every row" in summary
    assert "cost" in summary.lower()
