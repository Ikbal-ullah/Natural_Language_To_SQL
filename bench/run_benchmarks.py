"""Measure the numbers that go in BENCHMARKS.md and LEARN.md's cost table.

Three things, none of which needs a model:

  1. LATENCY DISTRIBUTION -- P50/P95 execution time over the 50 answerable golden
     reference queries, at the full 5,000,000-row scale. These are real analytical
     queries, not microbenchmarks, so the distribution is long-tailed and the P95 is the
     number that matters.

  2. SCALING CURVE -- one point-lookup and one aggregate, measured on copies of
     `submissions` at 10k / 100k / 1M / 5M rows, with and without an index. This is the
     part that is genuinely hard to reason about from first principles: an indexed lookup
     is roughly flat (a B-tree adds a level every ~250x), while an unindexed one is
     linear. Seeing both curves on the same table is the point.

  3. INDEX COST -- build time and storage per index, read back from the indexing
     exercise artifact.

Warm-cache medians throughout, for the reasons set out in db/indexing/run_exercise.py.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import psycopg  # noqa: E402

from eval.golden_set import GOLDEN_SET  # noqa: E402
from nl2sql.config import get_settings  # noqa: E402

ARTIFACT = ROOT / "bench" / "results.json"
INDEX_ARTIFACT = ROOT / "db" / "artifacts" / "index_exercise.json"
SCALES = (10_000, 100_000, 1_000_000, 5_000_000)
REPEATS = 5

SCALING_QUERIES = {
    "point_lookup": (
        "SELECT count(*) FROM {t} WHERE enrollment_id = 171300",
        "CREATE INDEX {t}_ix ON {t} (enrollment_id)",
    ),
    "range_aggregate": (
        "SELECT count(*), avg(score) FROM {t} "
        "WHERE submitted_at >= TIMESTAMPTZ '2024-02-01' "
        "AND submitted_at < TIMESTAMPTZ '2024-02-08'",
        "CREATE INDEX {t}_ix ON {t} (submitted_at)",
    ),
}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * p)))
    return ordered[k]


def time_query(cur: psycopg.Cursor[Any], sql: str, repeats: int = REPEATS) -> float:
    """Median server-side execution time in ms."""
    cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")  # warm
    cur.fetchone()
    times = []
    for _ in range(repeats):
        cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")
        row = cur.fetchone()
        payload = (row[0] if row else [{}])[0]
        times.append(float(payload.get("Execution Time", 0.0)))
    return statistics.median(times)


def measure_golden_latency(cur: psycopg.Cursor[Any]) -> dict[str, Any]:
    rows = []
    for q in GOLDEN_SET:
        if q.sql is None:
            continue
        ms = time_query(cur, q.sql, repeats=3)
        rows.append({"id": q.id, "category": q.category, "ms": round(ms, 2)})
        print(f"  {q.id:6} {q.category:14} {ms:9.2f} ms")
    times = [r["ms"] for r in rows]
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        by_cat.setdefault(str(r["category"]), []).append(float(r["ms"]))
    return {
        "queries": rows,
        "p50": round(statistics.median(times), 2),
        "p95": round(percentile(times, 0.95), 2),
        "max": round(max(times), 2),
        "min": round(min(times), 2),
        "by_category_p50": {
            c: round(statistics.median(v), 2) for c, v in sorted(by_cat.items())
        },
    }


def measure_scaling(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '600s'")
        cur.execute("SET jit = off")
        for scale in SCALES:
            table = f"bench_{scale}"
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            started = time.perf_counter()
            # ORDER BY id keeps the physical order time-correlated, matching the real
            # table; a random subset would change the scan economics.
            cur.execute(
                f"CREATE TABLE {table} AS SELECT enrollment_id, assignment_id, score, "
                f"submitted_at FROM submissions ORDER BY id LIMIT {scale}"
            )
            cur.execute(f"ANALYZE {table}")
            build = time.perf_counter() - started
            cur.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{table}'))")
            size_row = cur.fetchone()

            entry: dict[str, Any] = {"rows": scale, "size": size_row[0] if size_row else "?",
                                     "build_seconds": round(build, 1)}
            for name, (query_tpl, index_tpl) in SCALING_QUERIES.items():
                query = query_tpl.format(t=table)
                cur.execute(f"DROP INDEX IF EXISTS {table}_ix")
                cur.execute(f"ANALYZE {table}")
                without = time_query(cur, query)
                cur.execute(index_tpl.format(t=table))
                cur.execute(f"ANALYZE {table}")
                with_index = time_query(cur, query)
                cur.execute(f"SELECT pg_size_pretty(pg_relation_size('{table}_ix'))")
                ix_row = cur.fetchone()
                cur.execute(f"DROP INDEX IF EXISTS {table}_ix")
                entry[name] = {
                    "no_index_ms": round(without, 3),
                    "with_index_ms": round(with_index, 3),
                    "speedup": round(without / with_index, 1) if with_index else None,
                    "index_size": ix_row[0] if ix_row else "?",
                }
                print(f"  {scale:>9,} {name:16} "
                      f"no index {without:9.3f} ms   indexed {with_index:8.3f} ms")
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            out[str(scale)] = entry
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-scaling", action="store_true")
    ap.add_argument("--skip-latency", action="store_true")
    args = ap.parse_args()
    settings = get_settings()

    payload: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '600s'")
            cur.execute("SET jit = off")
            cur.execute("SELECT count(*) FROM submissions")
            row = cur.fetchone()
            payload["submission_rows"] = int(row[0]) if row else 0
            cur.execute(
                "SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
            row = cur.fetchone()
            payload["database_size"] = row[0] if row else "?"

            if not args.skip_latency:
                print("golden reference query latency at 5M rows:")
                payload["golden_latency"] = measure_golden_latency(cur)

        if not args.skip_scaling:
            print("\nscaling curve:")
            payload["scaling"] = measure_scaling(conn)

    if INDEX_ARTIFACT.exists():
        index_data = json.loads(INDEX_ARTIFACT.read_text())
        payload["index_cost"] = {
            "build_seconds": index_data["index_build_seconds"],
            "total_bytes": index_data["index_total_bytes"],
            "per_index": index_data["index_sizes_bytes"],
        }

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if "golden_latency" in payload:
        g = payload["golden_latency"]
        print(f"\nreference query latency  p50 {g['p50']} ms   p95 {g['p95']} ms   "
              f"max {g['max']} ms")
    print(f"wrote {ARTIFACT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
