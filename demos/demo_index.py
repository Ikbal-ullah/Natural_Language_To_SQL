"""Run one query with and without an index. Print both plans and both timings.

Run: python demos/demo_index.py
"""

import statistics
import time

import _bootstrap  # noqa: F401
import psycopg

from nl2sql.config import get_settings

QUERY = "SELECT count(*) FROM learners WHERE country_code = 'SE'"
INDEX = "CREATE INDEX demo_learners_country ON learners (country_code)"
REPEATS = 7

def measure(cur: psycopg.Cursor[tuple[object, ...]]) -> tuple[str, float]:
    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS) {QUERY}")  # warm the cache first
    times = []
    plan: list[str] = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS) {QUERY}")
        plan = [str(r[0]) for r in cur.fetchall()]
        times.append((time.perf_counter() - started) * 1000)
    return "\n".join(plan), statistics.median(times)

def main() -> int:
    with psycopg.connect(get_settings().admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS demo_learners_country")
        before_plan, before_ms = measure(cur)
        print(f"QUERY: {QUERY}\n")
        print("=" * 78, "\nWITHOUT INDEX\n", "=" * 78, sep="")
        print(before_plan, f"\n  median wall time: {before_ms:.2f} ms")
        cur.execute(INDEX)
        cur.execute("ANALYZE learners")
        after_plan, after_ms = measure(cur)
        print("\n" + "=" * 78, f"\nWITH {INDEX}\n", "=" * 78, sep="")
        print(after_plan, f"\n  median wall time: {after_ms:.2f} ms")
        cur.execute("SELECT pg_size_pretty(pg_relation_size('demo_learners_country'))")
        row = cur.fetchone()
        cur.execute("DROP INDEX IF EXISTS demo_learners_country")

    print("\n" + "-" * 78)
    print(f"speedup {before_ms / after_ms:.1f}x   index size {row[0] if row else '?'}")
    print("Read the plans, not the numbers: 'Seq Scan' became 'Index Only Scan', and the")
    print("Buffers line shows how many 8 kB pages each had to touch (1467 vs 3).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
