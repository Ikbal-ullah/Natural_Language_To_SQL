"""Migration runner.

Why migrations rather than one CREATE script
--------------------------------------------
A single schema.sql answers "what does the schema look like now". A migration directory
answers "how do I get from the schema that is currently in production to that one", which
is the question you actually have on the day you need it. It also gives you an ordered,
reviewable history: the composite foreign key in 0003 is a decision someone made on a
particular day for a particular reason, and it is legible as such.

The runner records applied versions in `schema_migrations`, so it is idempotent: running
it twice is a no-op, and running it after adding a file applies only the new file.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from nl2sql.config import get_settings

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "migrations"
# 0009 builds the performance indexes. It is held back so that the indexing exercise has
# an honest "before" state to measure. `--to 9` or `make index-exercise` applies it.
DEFAULT_TARGET = 8

_NAME_RE = re.compile(r"^(\d{4})_(.+)\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL
);
"""


def discover() -> list[tuple[int, str, pathlib.Path]]:
    """Return [(version, name, path)] sorted by version."""
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if not m:
            raise ValueError(f"migration filename must be NNNN_name.sql: {path.name}")
        found.append((int(m.group(1)), m.group(2), path))
    return found


def applied_versions(conn: psycopg.Connection[object]) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute("SELECT version FROM schema_migrations")
        return {int(r[0]) for r in cur.fetchall()}
    return set()


def run(target: int, *, dsn: str, verbose: bool = True) -> list[int]:
    """Apply every unapplied migration with version <= target. Returns versions applied."""
    done: list[int] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        have = applied_versions(conn)
        conn.commit()
        for version, name, path in discover():
            if version in have or version > target:
                continue
            body = path.read_text(encoding="utf-8")
            started = time.perf_counter()
            # Each migration runs in its own transaction: it applies completely or not at
            # all, and a failure halfway through 0004 cannot leave `assignments` created
            # but `submissions` missing.
            with conn.cursor() as cur:
                cur.execute(body)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, duration_ms) "
                    "VALUES (%s, %s, %s)",
                    (version, name, elapsed_ms),
                )
            conn.commit()
            done.append(version)
            if verbose:
                print(f"  applied {version:04d}_{name}  ({elapsed_ms} ms)")
    return done


def status(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        have = applied_versions(conn)
    print(f"{'ver':>4}  {'status':<9}  name")
    for version, name, _ in discover():
        mark = "applied" if version in have else "pending"
        print(f"{version:>4}  {mark:<9}  {name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply database migrations.")
    ap.add_argument("--to", type=int, default=DEFAULT_TARGET, help="highest version to apply")
    ap.add_argument("--status", action="store_true", help="show applied/pending and exit")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    dsn = args.dsn or get_settings().admin_dsn
    if args.status:
        status(dsn)
        return 0

    print(f"migrating {dsn} up to version {args.to}")
    done = run(args.to, dsn=dsn)
    print(f"done: {len(done)} migration(s) applied" if done else "done: already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
