"""Database access: connection pooling, the read-only execution path, and EXPLAIN.

WHY A POOL AT ALL (the long version is in LEARN.md, "Connection pooling")
------------------------------------------------------------------------
A PostgreSQL connection is not a socket, it is an operating-system process: the
postmaster forks a backend for every client, which costs roughly 1-3 ms of CPU and a few
megabytes of memory before you have run a single query. A web request that opens its own
connection therefore pays more to say hello than to do the work. Worse, the cost is
paid exactly when you can least afford it -- under load, when every request is opening
its own connection at once, and the machine spends its time forking instead of scanning.

A pool keeps a small number of backends alive and hands them out. The important
consequence is not just latency: it is that the pool is also an ADMISSION CONTROL. With
`max_size = 8`, at most eight queries can be in flight no matter how many HTTP requests
arrive. The ninth waits in a queue instead of adding a ninth concurrent seq scan to a
server that is already saturated. A server that is 100% busy on 8 queries finishes them;
a server thrashing between 800 finishes none. Sizing a pool larger than the database can
usefully serve makes throughput worse, not better -- this is the single most common
mistake with pools and the reason `pool_max_size` defaults to 8 here, not 100.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from nl2sql.config import Settings, get_settings

_POOLS: dict[str, ConnectionPool] = {}


def _make_pool(dsn: str, settings: Settings, *, read_only: bool) -> ConnectionPool:
    """Build a pool whose connections are pre-configured before first use.

    `configure` runs once per physical connection, not once per checkout, so the
    per-query overhead of these guarantees is zero.
    """

    def configure(conn: psycopg.Connection[Any]) -> None:
        conn.row_factory = dict_row
        with conn.cursor() as cur:
            # The timeout is set here AND on the role in migration 0008. Belt and
            # braces: a client that forgets, or a future caller that connects with a
            # different library, is still capped by the server-side role setting.
            cur.execute(
                pgsql.SQL("SET statement_timeout = {}").format(
                    pgsql.Literal(f"{settings.statement_timeout_ms}ms")
                )
            )
            if read_only:
                cur.execute("SET default_transaction_read_only = on")
                # Deterministic plan costs for the gate: JIT decisions add noise to the
                # estimate we compare against a fixed threshold.
                cur.execute("SET jit = off")
        conn.commit()

    return ConnectionPool(
        conninfo=dsn,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        configure=configure,
        open=True,
        timeout=10.0,
        name="ro" if read_only else "admin",
    )


def get_pool(*, read_only: bool = True, settings: Settings | None = None) -> ConnectionPool:
    """Return (creating on first use) the pool for the requested privilege level."""
    settings = settings or get_settings()
    key = "ro" if read_only else "admin"
    if key not in _POOLS:
        dsn = settings.readonly_dsn if read_only else settings.admin_dsn
        _POOLS[key] = _make_pool(dsn, settings, read_only=read_only)
    return _POOLS[key]


def close_pools() -> None:
    """Close every open pool. Used by tests and by FastAPI shutdown."""
    for pool in _POOLS.values():
        pool.close()
    _POOLS.clear()


@contextmanager
def readonly_cursor(settings: Settings | None = None) -> Iterator[psycopg.Cursor[Any]]:
    """Yield a cursor inside an explicitly read-only transaction.

    Two independent guarantees, because one is not enough:
      * the connection belongs to a role holding only SELECT (migration 0008), and
      * the transaction is opened read-only, so the server rejects a write even if the
        grants were somehow wrong.
    """
    pool = get_pool(read_only=True, settings=settings)
    with pool.connection() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            yield cur
        conn.rollback()


@contextmanager
def admin_cursor(settings: Settings | None = None) -> Iterator[psycopg.Cursor[Any]]:
    """Yield a cursor with DDL privileges. Migrations, seeding and index builds only."""
    pool = get_pool(read_only=False, settings=settings)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


# ---------------------------------------------------------------------------
# EXPLAIN
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanNode:
    """One node of a PostgreSQL plan tree, flattened to what the gate needs."""

    node_type: str
    relation: str | None
    total_cost: float
    plan_rows: float
    actual_rows: float | None
    depth: int
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _flatten(node: dict[str, Any], depth: int = 0) -> list[PlanNode]:
    out = [
        PlanNode(
            node_type=node.get("Node Type", "?"),
            relation=node.get("Relation Name"),
            total_cost=float(node.get("Total Cost", 0.0)),
            plan_rows=float(node.get("Plan Rows", 0.0)),
            actual_rows=(
                float(node["Actual Rows"]) if "Actual Rows" in node else None
            ),
            depth=depth,
            raw=node,
        )
    ]
    for child in node.get("Plans", []) or []:
        out.extend(_flatten(child, depth + 1))
    return out


@dataclass(frozen=True)
class Plan:
    """A parsed EXPLAIN result."""

    nodes: list[PlanNode]
    total_cost: float
    plan_rows: float
    execution_ms: float | None
    planning_ms: float | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def root(self) -> PlanNode:
        return self.nodes[0]

    def text_tree(self) -> str:
        """Render the plan the way `EXPLAIN` would, for display and for the docs."""
        lines = []
        for n in self.nodes:
            prefix = "  " * n.depth + ("-> " if n.depth else "")
            rel = f" on {n.relation}" if n.relation else ""
            actual = f" (actual rows={n.actual_rows:.0f})" if n.actual_rows is not None else ""
            lines.append(
                f"{prefix}{n.node_type}{rel}  "
                f"(cost={n.total_cost:.2f} rows={n.plan_rows:.0f}){actual}"
            )
        return "\n".join(lines)


def explain(
    query: str,
    params: Sequence[Any] | None = None,
    *,
    analyze: bool = False,
    settings: Settings | None = None,
) -> Plan:
    """Run EXPLAIN (FORMAT JSON) and return a parsed plan.

    `analyze=False` is the default and the one the plan gate uses: it asks the planner
    what it INTENDS to do without running the query. That distinction is the whole point
    of the gate -- we must be able to refuse an expensive query without first paying for
    it. `analyze=True` actually executes and is used by the teaching artifacts, where we
    want to compare the estimate against reality.
    """
    opts = "FORMAT JSON, VERBOSE FALSE" + (", ANALYZE TRUE, BUFFERS TRUE" if analyze else "")
    stmt = f"EXPLAIN ({opts}) {query}"
    with readonly_cursor(settings) as cur:
        cur.execute(stmt, params)
        row = cur.fetchone()
    if row is None:  # pragma: no cover - EXPLAIN always returns a row
        raise RuntimeError("EXPLAIN returned no rows")
    payload = row["QUERY PLAN"] if isinstance(row, dict) else row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    top = payload[0]
    plan = top["Plan"]
    return Plan(
        nodes=_flatten(plan),
        total_cost=float(plan.get("Total Cost", 0.0)),
        plan_rows=float(plan.get("Plan Rows", 0.0)),
        execution_ms=top.get("Execution Time"),
        planning_ms=top.get("Planning Time"),
        raw=top,
    )


def fetch_all(
    query: str,
    params: Sequence[Any] | None = None,
    *,
    settings: Settings | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute a read-only query, returning (column names, rows as tuples).

    Rows come back as tuples rather than dicts because the evaluation comparator needs to
    treat a result as a multiset of ordered values, and because two correct queries may
    legitimately name their columns differently.
    """
    with readonly_cursor(settings) as cur:
        cur.row_factory = psycopg.rows.tuple_row
        cur.execute(query, params)
        cols = [d.name for d in cur.description or []]
        rows = cur.fetchall()
    return cols, [tuple(r) for r in rows]


def table_row_estimates(settings: Settings | None = None) -> dict[str, int]:
    """Live-row estimate per table, straight from the planner's own statistics.

    The plan gate needs to know which tables are 'large'. Asking `pg_class.reltuples`
    costs nothing and, crucially, uses the SAME numbers the planner used, so the gate and
    the planner never disagree about how big a table is.
    """
    sql_text = """
        SELECT c.relname AS table_name, GREATEST(c.reltuples, 0)::bigint AS rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
    """
    with readonly_cursor(settings) as cur:
        cur.execute(sql_text)
        return {r["table_name"]: int(r["rows"]) for r in cur.fetchall()}
