"""N+1 queries: why the bug hides on localhost and explodes in production.
Run: python demos/demo_n_plus_one.py
"""

import time

import _bootstrap  # noqa: F401
import psycopg

from nl2sql.config import get_settings

# Round trip a real deployment pays per query. Loopback is ~0.05 ms, which is exactly why
# this bug survives every local test you will ever write.
RTT_MS = {"localhost": 0.0, "same datacentre (0.5 ms)": 0.5, "cross-AZ (3 ms)": 3.0}
VERDICT = """
Both approaches return identical rows. On loopback N+1 can even look FASTER: one GROUP BY
over 210k enrollments beats 40 indexed lookups -- so the bug is invisible locally. Add a
real network and cost is per round trip: it grows with N, the JOIN stays at one."""

class Counting:
    """Counts execute() calls -- all an ORM query profiler does."""
    def __init__(self, cur: psycopg.Cursor[tuple[object, ...]]) -> None:
        self.cur, self.queries = cur, 0

    def run(self, sql: str, args: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        self.queries += 1
        self.cur.execute(sql, args)
        return list(self.cur.fetchall())

def n_plus_one(c: Counting, n: int) -> list[tuple[object, ...]]:
    """1 query for the list, then 1 more per row -- the default shape of every ORM."""
    rows = c.run("SELECT id, title FROM courses ORDER BY id LIMIT %s", (n,))
    q = "SELECT count(*) FROM enrollments WHERE course_id = %s"
    return [(t, c.run(q, (cid,))[0][0]) for cid, t in rows]

def single_join(c: Counting, n: int) -> list[tuple[object, ...]]:
    """The same answer asked once: the database does the loop, not the network."""
    q = ("SELECT c.title, count(e.id) FROM courses c LEFT JOIN enrollments e "
         "ON e.course_id = c.id WHERE c.id <= %s GROUP BY c.id, c.title ORDER BY c.id")
    return c.run(q, (n,))

def main() -> int:
    head = "  ".join(f"{k:>22}" for k in RTT_MS)
    print(f"{'N':>5} {'approach':<12} {'queries':>8} {'measured':>10}   {head}")

    with psycopg.connect(get_settings().readonly_dsn) as conn, conn.cursor() as cur:
        for n in (5, 20, 40):
            seen: list[list[tuple[object, ...]]] = []
            for name, fn in (("N+1", n_plus_one), ("single JOIN", single_join)):
                c, t0 = Counting(cur), time.perf_counter()
                seen.append(sorted(fn(c, n)))
                ms = (time.perf_counter() - t0) * 1000
                mdl = "  ".join(f"{ms + c.queries * r:>19.1f} ms" for r in RTT_MS.values())
                print(f"{n:>5} {name:<12} {c.queries:>8} {ms:>7.2f} ms   {mdl}")
            assert seen[0] == seen[1], "both approaches must agree"
    print(VERDICT)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
