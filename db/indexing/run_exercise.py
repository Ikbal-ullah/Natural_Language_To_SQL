"""The indexing exercise: measure ten queries, add indexes, measure again.

This is the teaching artifact the whole database half of the repo is built around, so it
is worth being precise about the methodology -- a benchmark you cannot defend is worse
than no benchmark, because you will believe it.

WHAT IS MEASURED
  EXPLAIN (ANALYZE, BUFFERS) for each query, before and after the indexes in migration
  0009 exist. We record the plan tree, the planner's estimate, the actual row count, the
  buffers touched, and the wall-clock execution time.

WHY WARM-CACHE NUMBERS
  Each query is run once to warm the buffer cache and then REPEATS times for timing, and
  we report the median. Cold-cache numbers are dominated by whether the page happened to
  be in the OS cache, which on a laptop is mostly a measure of what you ran last. Warm
  numbers isolate the thing the index actually changes: how many pages have to be visited
  at all. The `shared_blks_read` vs `shared_blks_hit` split is recorded so the difference
  between "fewer pages" and "faster pages" stays visible.

WHY MEDIAN AND NOT MEAN
  One outlier -- a checkpoint, an autovacuum, the OS scheduling something else -- moves a
  mean of five runs by a lot and moves the median by nothing.

WHAT THIS DOES NOT MEASURE
  Write cost. Every index here makes INSERTs and UPDATEs to its table slower and takes
  storage. The exercise records index build time and size so that side of the ledger is at
  least visible; LEARN.md's cost table works it through.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from nl2sql.config import get_settings  # noqa: E402

QUERIES_FILE = ROOT / "db" / "indexing" / "queries.sql"
INDEX_MIGRATION = ROOT / "migrations" / "0009_performance_indexes.sql"
ARTIFACT_DIR = ROOT / "db" / "artifacts"
REPEATS = 5

_NAME_RE = re.compile(r"^--\s*name:\s*(\S+)", re.MULTILINE)
_LESSON_RE = re.compile(r"^--\s*lesson:\s*(.*)$", re.MULTILINE)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+INDEX\s+(?:CONCURRENTLY\s+)?(\w+)\s+ON", re.IGNORECASE
)


@dataclass
class Measurement:
    plan_text: str
    plan_json: dict[str, Any] = field(repr=False, default_factory=dict)
    estimated_cost: float = 0.0
    estimated_rows: float = 0.0
    actual_rows: float = 0.0
    median_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    shared_hit: int = 0
    shared_read: int = 0
    node_types: list[str] = field(default_factory=list)


@dataclass
class QueryResult:
    name: str
    lesson: str
    sql: str
    before: Measurement
    after: Measurement

    @property
    def speedup(self) -> float:
        if self.after.median_ms <= 0:
            return float("inf")
        return self.before.median_ms / self.after.median_ms


def _fmt(ms: float) -> str:
    """Sub-millisecond timings need more decimals than millisecond ones.

    Printing 0.00 ms for a 6 microsecond index lookup hides the very thing the index did.
    """
    if ms >= 10:
        return f"{ms:.1f} ms"
    if ms >= 1:
        return f"{ms:.2f} ms"
    return f"{ms * 1000:.0f} us"


def parse_queries(path: pathlib.Path) -> list[tuple[str, str, str]]:
    """Return [(name, lesson, sql)] from the annotated SQL file."""
    text = path.read_text(encoding="utf-8")
    blocks: list[tuple[str, str, str]] = []
    positions = [(m.start(), m.group(1)) for m in _NAME_RE.finditer(text)]
    for i, (start, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        chunk = text[start:end]
        lesson = " ".join(_LESSON_RE.findall(chunk)).strip()
        sql_lines = [
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ]
        sql = "\n".join(sql_lines).strip().rstrip(";").strip()
        if sql:
            blocks.append((name, lesson, sql))
    return blocks


def index_names() -> list[str]:
    return _CREATE_INDEX_RE.findall(INDEX_MIGRATION.read_text(encoding="utf-8"))


def collect_node_types(node: dict[str, Any], out: list[str]) -> None:
    label = node.get("Node Type", "?")
    if rel := node.get("Relation Name"):
        label = f"{label} on {rel}"
    out.append(label)
    for child in node.get("Plans", []) or []:
        collect_node_types(child, out)


def render_plan(node: dict[str, Any], depth: int = 0) -> list[str]:
    prefix = "  " * depth + ("-> " if depth else "")
    rel = f" on {node['Relation Name']}" if node.get("Relation Name") else ""
    idx = f" using {node['Index Name']}" if node.get("Index Name") else ""
    cost = (
        f"(cost={node.get('Startup Cost', 0):.2f}..{node.get('Total Cost', 0):.2f} "
        f"rows={node.get('Plan Rows', 0)})"
    )
    actual = ""
    if "Actual Total Time" in node:
        actual = (
            f" (actual time={node.get('Actual Startup Time', 0):.3f}.."
            f"{node['Actual Total Time']:.3f} rows={node.get('Actual Rows', 0)} "
            f"loops={node.get('Actual Loops', 1)})"
        )
    lines = [f"{prefix}{node.get('Node Type', '?')}{rel}{idx}  {cost}{actual}"]
    if cond := (node.get("Index Cond") or node.get("Recheck Cond")):
        lines.append("  " * (depth + 1) + f"Index Cond: {cond}")
    if filt := node.get("Filter"):
        lines.append("  " * (depth + 1) + f"Filter: {filt}")
    for child in node.get("Plans", []) or []:
        lines.extend(render_plan(child, depth + 1))
    return lines


def measure(conn: psycopg.Connection[Any], sql: str) -> Measurement:
    with conn.cursor() as cur:
        # Warm-up run. The first execution of anything on a laptop pays for reading pages
        # off disk, which is a property of the machine, not of the query.
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
        cur.fetchone()

        # The server's own Execution Time is collected from EVERY repeat, not just the
        # last one. Measuring once and reporting it five times would make the spread look
        # like zero and quietly turn REPEATS into decoration.
        execution_times: list[float] = []
        payload: dict[str, Any] = {}
        for _ in range(REPEATS):
            wall_start = time.perf_counter()
            cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
            row = cur.fetchone()
            wall_ms = (time.perf_counter() - wall_start) * 1000
            raw = row[0] if row else []
            payload = raw[0] if isinstance(raw, list) else json.loads(raw)[0]
            execution_times.append(float(payload.get("Execution Time", wall_ms)))

    plan = payload.get("Plan", {})
    node_types: list[str] = []
    collect_node_types(plan, node_types)
    return Measurement(
        plan_text="\n".join(render_plan(plan)),
        plan_json=payload,
        estimated_cost=float(plan.get("Total Cost", 0.0)),
        estimated_rows=float(plan.get("Plan Rows", 0.0)),
        actual_rows=float(plan.get("Actual Rows", 0.0)),
        median_ms=float(statistics.median(execution_times)),
        min_ms=float(min(execution_times)),
        max_ms=float(max(execution_times)),
        shared_hit=int(plan.get("Shared Hit Blocks", 0)),
        shared_read=int(plan.get("Shared Read Blocks", 0)),
        node_types=node_types,
    )


def drop_indexes(conn: psycopg.Connection[Any], names: list[str]) -> None:
    with conn.cursor() as cur:
        for name in names:
            cur.execute(f"DROP INDEX IF EXISTS {name}")
        cur.execute("ANALYZE")
    conn.commit()


def build_indexes(conn: psycopg.Connection[Any]) -> tuple[float, dict[str, int]]:
    """Apply the index migration, returning build seconds and per-index size in bytes."""
    body = INDEX_MIGRATION.read_text(encoding="utf-8")
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(body)
    conn.commit()
    elapsed = time.perf_counter() - started

    sizes: dict[str, int] = {}
    with conn.cursor() as cur:
        for name in index_names():
            cur.execute("SELECT pg_relation_size(to_regclass(%s))", (name,))
            row = cur.fetchone()
            sizes[name] = int(row[0]) if row and row[0] is not None else 0
    return elapsed, sizes


def record_migration(conn: psycopg.Connection[Any], duration_ms: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (version, name, duration_ms) "
            "VALUES (9, 'performance_indexes', %s) ON CONFLICT (version) DO NOTHING",
            (duration_ms,),
        )
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=None)
    ap.add_argument(
        "--keep-indexes",
        action="store_true",
        help="skip the 'before' phase and only measure the current, indexed state",
    )
    args = ap.parse_args()
    dsn = args.dsn or get_settings().admin_dsn

    queries = parse_queries(QUERIES_FILE)
    names = index_names()
    print(f"indexing exercise: {len(queries)} queries, {len(names)} indexes\n")

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[QueryResult] = []

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # These queries are deliberately slow before indexing; the 5 s application
            # timeout would abort them and we would measure the timeout, not the query.
            cur.execute("SET statement_timeout = '180s'")
            cur.execute("SET jit = off")
        conn.commit()

        if not args.keep_indexes:
            print("phase 1: dropping performance indexes for an honest baseline")
            drop_indexes(conn, names)

        print("phase 2: measuring BEFORE")
        before: dict[str, Measurement] = {}
        for name, _lesson, sql in queries:
            m = measure(conn, sql)
            before[name] = m
            print(f"  {name:30} {_fmt(m.median_ms):>11}   {m.node_types[0][:44]}")

        print("\nphase 3: building indexes")
        build_seconds, sizes = build_indexes(conn)
        record_migration(conn, int(build_seconds * 1000))
        total_bytes = sum(sizes.values())
        print(f"  {len(names)} indexes built in {build_seconds:.1f}s, "
              f"{total_bytes / 1024 / 1024:.1f} MB total")

        print("\nphase 4: measuring AFTER")
        for name, lesson, sql in queries:
            m = measure(conn, sql)
            results.append(QueryResult(name, lesson, sql, before[name], m))
            speed = results[-1].speedup
            arrow = "faster" if speed >= 1 else "SLOWER"
            print(
                f"  {name:30} {_fmt(m.median_ms):>11}   "
                f"{speed:8.1f}x {arrow}   {m.node_types[0][:34]}"
            )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
            row = cur.fetchone()
            db_size = row[0] if row else "?"

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repeats": REPEATS,
        "database_size": db_size,
        "index_build_seconds": round(build_seconds, 2),
        "index_sizes_bytes": sizes,
        "index_total_bytes": total_bytes,
        "queries": [
            {
                "name": r.name,
                "lesson": r.lesson,
                "sql": r.sql,
                "speedup": round(r.speedup, 1) if r.speedup != float("inf") else None,
                "before": {k: v for k, v in asdict(r.before).items() if k != "plan_json"},
                "after": {k: v for k, v in asdict(r.after).items() if k != "plan_json"},
            }
            for r in results
        ],
    }
    out = ARTIFACT_DIR / "index_exercise.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out.relative_to(ROOT)}")

    speedups = [r.speedup for r in results if r.speedup != float("inf")]
    print(
        f"median speedup {statistics.median(speedups):.1f}x, "
        f"best {max(speedups):.1f}x, worst {min(speedups):.1f}x"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
