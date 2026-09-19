"""Seed the learning platform with production-shaped data.

Two things make this different from a toy fixture, and both exist so that query plans
behave the way they would in production:

1. SKEW.  Real usage is never uniform.  Most learners submit a handful of times; a few
   thousand submit hundreds.  Uniform random data makes every histogram flat, which makes
   the planner's estimates trivially correct, which means you never get to see an estimate
   go wrong -- and "the estimate went wrong" is the cause of most real slow queries.  The
   submission counts here are lognormal, so the top 1% of enrollments own roughly a
   quarter of all submissions.

2. PHYSICAL ORDER.  Submissions are written in `submitted_at` order, because in
   production rows arrive as the events happen.  That correlation is what makes a BRIN
   index viable (migration 0009) and it changes the cost of a range scan by orders of
   magnitude.  Generating them grouped by learner instead would scatter timestamps across
   the heap and quietly invalidate one of the lessons in this repo, so the generator
   bucket-sorts rows to disk by week and loads the buckets in chronological order.

Everything is driven by a fixed RNG seed, so two runs produce byte-identical data and the
golden evaluation set has stable expected answers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import random
import shutil
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from nl2sql.config import get_settings

SEED = 20250909
EPOCH = dt.datetime(2022, 1, 3, tzinfo=dt.UTC)  # a Monday
HORIZON_DAYS = 1_100

SUBJECTS = {
    "Programming": ["Python", "C++", "Rust", "Go", "JavaScript", "Systems Programming"],
    "Data": ["SQL", "Data Modelling", "Pandas", "Statistics", "Spark", "Warehousing"],
    "Security": ["Cryptography", "Web Security", "Reverse Engineering", "Cloud Security"],
    "Design": ["UX Research", "Interaction Design", "Design Systems"],
    "Infrastructure": ["Linux", "Kubernetes", "Networking", "Databases", "Observability"],
}
DIFFICULTIES = ["beginner", "intermediate", "advanced"]
COUNTRIES = [
    ("US", 0.24), ("IN", 0.17), ("GB", 0.08), ("DE", 0.07), ("BR", 0.06),
    ("NG", 0.05), ("CA", 0.05), ("FR", 0.04), ("PL", 0.04), ("ES", 0.03),
    ("AU", 0.03), ("JP", 0.03), ("KE", 0.03), ("MX", 0.03), ("ID", 0.02),
    ("VN", 0.02), ("SE", 0.01),
]
TZ_BY_COUNTRY = {
    "US": "America/New_York", "IN": "Asia/Kolkata", "GB": "Europe/London",
    "DE": "Europe/Berlin", "BR": "America/Sao_Paulo", "NG": "Africa/Lagos",
    "CA": "America/Toronto", "FR": "Europe/Paris", "PL": "Europe/Warsaw",
    "ES": "Europe/Madrid", "AU": "Australia/Sydney", "JP": "Asia/Tokyo",
    "KE": "Africa/Nairobi", "MX": "America/Mexico_City", "ID": "Asia/Jakarta",
    "VN": "Asia/Ho_Chi_Minh", "SE": "Europe/Stockholm",
}
FIRST = [
    "Ana", "Ben", "Chen", "Dara", "Eli", "Fatima", "Gita", "Hugo", "Ines", "Jae",
    "Kwame", "Lena", "Milo", "Nina", "Omar", "Pia", "Quinn", "Rosa", "Sami", "Tara",
    "Uma", "Viktor", "Wei", "Xime", "Yusuf", "Zara", "Aditi", "Bruno", "Cleo", "Dmitri",
]
LAST = [
    "Adeyemi", "Bakshi", "Costa", "Dubois", "Eriksen", "Ferrari", "Gomez", "Haddad",
    "Ivanov", "Jensen", "Kowalski", "Lindqvist", "Mensah", "Nakamura", "Okafor",
    "Patel", "Quintero", "Rossi", "Silva", "Tanaka", "Ueda", "Vargas", "Wong",
    "Xu", "Yilmaz", "Zhang", "Novak", "Muller", "Sharma", "Fernandez",
]
ASSIGNMENT_TITLES = [
    "Warm-up", "Fundamentals Quiz", "First Program", "Refactor Exercise",
    "Data Structures Drill", "Midterm Project", "Debugging Lab", "Performance Lab",
    "Design Writeup", "Integration Exercise", "Peer Review", "Capstone Part 1",
    "Capstone Part 2", "Final Assessment", "Stretch Challenge", "Retrospective Essay",
]


def weighted_pick(rng: random.Random, options: list[tuple[str, float]]) -> str:
    r = rng.random()
    acc = 0.0
    for value, weight in options:
        acc += weight
        if r <= acc:
            return value
    return options[-1][0]


def ts(day_offset: float) -> str:
    return (EPOCH + dt.timedelta(days=day_offset)).isoformat(sep=" ")


def esc(text: str) -> str:
    """Escape a value for COPY text format."""
    return text.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ")


class Loader:
    """Streams TSV lines into PostgreSQL with COPY.

    COPY rather than INSERT because it is roughly 20x faster: one parse, one round trip
    and no per-row statement overhead.  Loading 5M rows with INSERT takes long enough that
    you would stop re-running the seed, and a seed you do not re-run is a seed that rots.
    """

    def __init__(self, conn: psycopg.Connection[object], batch: int = 20_000) -> None:
        self.conn = conn
        self.batch = batch

    def load(self, table: str, columns: list[str], rows: Iterator[list[str]]) -> int:
        stmt = f"COPY {table} ({', '.join(columns)}) FROM STDIN"
        count = 0
        buf: list[str] = []
        with self.conn.cursor() as cur, cur.copy(stmt) as copy:  # type: ignore[arg-type]
            for row in rows:
                buf.append("\t".join(row))
                count += 1
                if len(buf) >= self.batch:
                    copy.write("\n".join(buf) + "\n")
                    buf.clear()
            if buf:
                copy.write("\n".join(buf) + "\n")
        return count


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------

def gen_courses(rng: random.Random, n: int) -> list[dict[str, object]]:
    courses = []
    pairs = [(s, t) for s, topics in SUBJECTS.items() for t in topics]
    rng.shuffle(pairs)
    for i in range(n):
        subject, topic = pairs[i % len(pairs)]
        level = i // len(pairs) + 1
        title = f"{topic}" if level == 1 else f"{topic} {level}"
        difficulty = DIFFICULTIES[min(2, (i // 7) % 3)]
        courses.append(
            {
                "id": i + 1,
                "slug": f"{topic.lower().replace(' ', '-')}-{level}",
                "title": title,
                "subject": subject,
                "difficulty": difficulty,
                "list_price_cents": rng.choice([9900, 14900, 19900, 24900, 29900, 39900]),
                "duration_weeks": rng.choice([4, 6, 8, 8, 10, 12]),
                "is_active": rng.random() > 0.12,
                "published_at": ts(rng.uniform(0, 200)),
            }
        )
    return courses


def gen_cohorts(
    rng: random.Random, courses: list[dict[str, object]], per_course: int
) -> list[dict[str, object]]:
    cohorts = []
    cid = 0
    # Derive the spacing from the horizon rather than hardcoding it, so a scaled-down
    # dataset still spans the same period and still contains finished, running and
    # scheduled cohorts. A fixed spacing packs small datasets into the first months and
    # quietly makes cohorts.status a single-valued column.
    # The last intake must start AFTER the simulated 'today' or there are no scheduled
    # cohorts at all, so the divisor is (per_course - 1) and the numerator overshoots.
    spacing = max(14, int((HORIZON_DAYS + 140) / max(1, per_course - 1)))
    for course in courses:
        weeks = int(course["duration_weeks"])  # type: ignore[call-overload]
        for k in range(per_course):
            cid += 1
            # Spaced so that the 16 intakes of a course span the entire simulated
            # horizon and past its end: the dataset then contains finished,
            # running AND scheduled cohorts, which is what a real catalogue looks
            # like. Packing them into the first year -- the obvious thing to do --
            # silently makes cohorts.status a two-valued column and makes every
            # question about upcoming cohorts unanswerable.
            start_day = 20 + k * spacing + rng.randint(0, spacing // 4)
            start = EPOCH.date() + dt.timedelta(days=start_day)
            end = start + dt.timedelta(weeks=weeks)
            today = EPOCH.date() + dt.timedelta(days=HORIZON_DAYS)
            if end < today:
                status = "cancelled" if rng.random() < 0.04 else "finished"
            elif start <= today:
                status = "running"
            else:
                status = "scheduled"
            cohorts.append(
                {
                    "id": cid,
                    "course_id": course["id"],
                    "name": f"{start.year}-W{start.isocalendar().week:02d}",
                    "starts_on": start.isoformat(),
                    "ends_on": end.isoformat(),
                    "capacity": rng.choice([30, 40, 50, 60, 80, 120]),
                    "status": status,
                    "start_day": start_day,
                    "weeks": weeks,
                }
            )
    return cohorts


def gen_assignments(
    rng: random.Random, courses: list[dict[str, object]]
) -> list[dict[str, object]]:
    out = []
    aid = 0
    for course in courses:
        n = rng.randint(12, 16)
        for pos in range(1, n + 1):
            aid += 1
            kind = "quiz" if pos % 5 == 1 else "coding" if pos % 5 in (2, 3) else (
                "essay" if pos % 5 == 4 else "project"
            )
            weeks = int(course["duration_weeks"])  # type: ignore[call-overload]
            out.append(
                {
                    "id": aid,
                    "course_id": course["id"],
                    "position": pos,
                    "title": f"{ASSIGNMENT_TITLES[(pos - 1) % len(ASSIGNMENT_TITLES)]}",
                    "kind": kind,
                    "max_score": 100 if kind != "quiz" else 20,
                    "due_offset_days": int(pos * (weeks * 7) / (n + 1)),
                    "is_optional": pos % 9 == 0,
                    # Hard assignments attract retries; this is what creates a realistic
                    # non-uniform distribution of attempt_no.
                    "retry_weight": 1.0 + (3.5 if kind in ("coding", "project") else 0.2)
                    * rng.random(),
                }
            )
    return out


def gen_learner_rows(rng: random.Random, n: int) -> Iterator[list[str]]:
    """Signups grow over time, so the table has a realistic vintage distribution."""
    for i in range(1, n + 1):
        first = FIRST[rng.randrange(len(FIRST))]
        last = LAST[rng.randrange(len(LAST))]
        country = weighted_pick(rng, COUNTRIES)
        # sqrt-biased towards recent: a growing platform has more recent accounts.
        signup_day = HORIZON_DAYS * math.sqrt(rng.random()) * 0.97
        r = rng.random()
        status = (
            "active" if r < 0.62 else "paused" if r < 0.77 else "churned" if r < 0.995
            else "banned"
        )
        if status in ("churned", "banned"):
            last_active = signup_day + rng.uniform(1, 120)
        else:
            last_active = signup_day + rng.uniform(0, HORIZON_DAYS - signup_day)
        last_active = min(last_active, HORIZON_DAYS)
        # ~3% of accounts never come back after signing up. learners.last_active_ts is
        # documented as "NULL means never active since signup", and that comment is fed
        # verbatim to the model -- so the data has to actually contain NULLs or the prompt
        # is describing a column that does not exist as described.
        # Keyed off the id rather than the RNG so that adding this does not consume a
        # random draw and shift every other generated value downstream.
        never_active = i % 33 == 0
        yield [
            str(i),
            f"{first.lower()}.{last.lower()}{i}@example.com",
            esc(f"{first} {last}"),
            country,
            TZ_BY_COUNTRY[country],
            status,
            "t" if rng.random() < 0.02 else "f",
            ts(signup_day),
            r"\N" if never_active else ts(last_active),
        ]


def gen_enrollments(
    rng: random.Random,
    n_learners: int,
    cohorts: list[dict[str, object]],
    mentor_ids: list[int],
) -> list[dict[str, object]]:
    """Enrollment counts per learner are skewed: most take one course, a few take many."""
    by_id = {int(c["id"]): c for c in cohorts}  # type: ignore[call-overload]
    open_cohorts = [c for c in cohorts if c["status"] != "cancelled"]
    enrollments: list[dict[str, object]] = []
    eid = 0
    for learner_id in range(1, n_learners + 1):
        r = rng.random()
        k = 1 if r < 0.46 else 2 if r < 0.72 else 3 if r < 0.87 else 4 if r < 0.95 else (
            rng.randint(5, 9)
        )
        chosen: set[int] = set()
        for _ in range(k):
            cohort = open_cohorts[rng.randrange(len(open_cohorts))]
            if int(cohort["id"]) in chosen:  # type: ignore[call-overload]
                continue
            chosen.add(int(cohort["id"]))  # type: ignore[call-overload]
            eid += 1
            start_day = float(by_id[int(cohort["id"])]["start_day"])  # type: ignore[call-overload,arg-type]
            weeks = int(by_id[int(cohort["id"])]["weeks"])  # type: ignore[call-overload,arg-type]
            enrolled_day = min(start_day - rng.uniform(0, 21), float(HORIZON_DAYS))
            p = rng.random()
            if cohort["status"] == "finished":
                status = "completed" if p < 0.58 else "dropped" if p < 0.93 else "deferred"
            elif cohort["status"] == "running":
                status = "active" if p < 0.86 else "dropped"
            else:
                status = "active"
            completed_day = start_day + weeks * 7 + rng.uniform(-3, 5) if status == "completed" else None
            enrollments.append(
                {
                    "id": eid,
                    "learner_id": learner_id,
                    "cohort_id": int(cohort["id"]),  # type: ignore[call-overload]
                    "course_id": int(cohort["course_id"]),  # type: ignore[call-overload]
                    "status": status,
                    "enrolled_at": max(0.0, enrolled_day),
                    "completed_at": completed_day,
                    # Grades are normal-ish and clipped, so percentile/window queries in
                    # the golden set have a sensible distribution to work with.
                    "final_grade": (
                        None if status != "completed"
                        else round(min(100.0, max(0.0, rng.gauss(74, 13))), 2)
                    ),
                    "start_day": start_day,
                    "weeks": weeks,
                }
            )
    _ = mentor_ids
    return enrollments


def enrollment_rows(enrollments: list[dict[str, object]]) -> Iterator[list[str]]:
    for e in enrollments:
        yield [
            str(e["id"]),
            str(e["learner_id"]),
            str(e["cohort_id"]),
            str(e["course_id"]),
            str(e["status"]),
            ts(float(e["enrolled_at"])),  # type: ignore[arg-type]
            r"\N" if e["completed_at"] is None else ts(float(e["completed_at"])),  # type: ignore[arg-type]
            r"\N" if e["final_grade"] is None else str(e["final_grade"]),
        ]


MAX_SUBMISSIONS_PER_ENROLLMENT = 480  # <= (min assignments per course) * 50, the UNIQUE cap


def submission_counts(
    rng: random.Random,
    n_enrollments: int,
    target: int,
    caps: list[int] | None = None,
    weights: list[float] | None = None,
) -> list[int]:
    """Lognormal counts, rescaled to hit the target total exactly.

    Lognormal because that is empirically what per-user activity looks like: the log of
    the count is roughly normal, so the mean sits well above the median and a small
    fraction of users dominate the total.  With mu=2.0, sigma=1.25 the top 1% of
    enrollments carry about a quarter of all submissions -- which is precisely the shape
    that makes the planner's default 'rows are uniformly distributed' assumption wrong,
    and therefore the shape you need in order to learn anything from EXPLAIN.
    """
    raw = [math.exp(rng.gauss(2.0, 1.25)) for _ in range(n_enrollments)]
    if weights is not None:
        # An enrollment in a cohort that starts next month cannot have submissions yet.
        # Weighting by the elapsed fraction of the cohort keeps future cohorts empty
        # without distorting the lognormal shape of the enrollments that have run.
        raw = [v * w for v, w in zip(raw, weights, strict=True)]
    total = sum(raw)
    if total <= 0:
        return [0] * n_enrollments
    scale = target / total
    limits = caps or [MAX_SUBMISSIONS_PER_ENROLLMENT] * n_enrollments
    counts = [min(limits[i], max(0, int(v * scale))) for i, v in enumerate(raw)]
    # Fix the rounding residual so the totals land exactly on target rather than
    # 'about 5 million'. Top-ups respect each enrollment's own capacity -- the number of
    # assignments already due times the 50-attempt limit of the UNIQUE constraint -- so
    # no row is generated that the schema would refuse and none is silently dropped.
    residual = target - sum(counts)
    headroom = sum(limits) - sum(counts)
    if residual > headroom:
        raise ValueError(
            f"cannot place {residual:,} more submissions: only {headroom:,} slots remain. "
            "Lower --scale or lengthen the simulated horizon."
        )
    guard = 0
    while residual > 0 and guard < target * 20:
        i = rng.randrange(n_enrollments)
        guard += 1
        if counts[i] < limits[i]:
            counts[i] += 1
            residual -= 1
    while residual < 0:
        i = rng.randrange(n_enrollments)
        if counts[i] > 0:
            counts[i] -= 1
            residual += 1
    return counts


def redistribute(picks: Counter[int], k: int, cap: int = 50) -> Counter[int]:
    """Move attempts beyond the UNIQUE cap onto other assignments instead of dropping them.

    UNIQUE (enrollment_id, assignment_id, attempt_no) allows at most `cap` attempts at one
    assignment. Because per-enrollment totals are themselves capped below k*cap, there is
    always somewhere for the overflow to go.
    """
    overflow = 0
    for idx in list(picks):
        if picks[idx] > cap:
            overflow += picks[idx] - cap
            picks[idx] = cap
    if not overflow:
        return picks
    for idx in range(k):
        if overflow <= 0:
            break
        room = cap - picks.get(idx, 0)
        if room > 0:
            take = min(room, overflow)
            picks[idx] = picks.get(idx, 0) + take
            overflow -= take
    return picks


def write_submission_buckets(
    rng: random.Random,
    enrollments: list[dict[str, object]],
    assignments: list[dict[str, object]],
    target: int,
    workdir: pathlib.Path,
) -> tuple[int, int]:
    """Generate submissions and bucket-sort them to disk by week of submitted_at.

    Held on disk rather than in memory because 5M Python tuples cost well over a gigabyte
    of RAM, and the machine that runs the seed is usually also the machine running the
    database.  The buckets are then loaded in chronological order so that the heap ends up
    physically correlated with submitted_at -- see the module docstring.
    """
    by_course: dict[int, list[dict[str, object]]] = {}
    for a in assignments:
        by_course.setdefault(int(a["course_id"]), []).append(a)  # type: ignore[call-overload]

    # Weight and cap each enrollment by how many of its assignments are actually due.
    # Deriving both from the same quantity is what makes the loaded total exact: an
    # enrollment that can hold no rows is also allocated none.
    weights: list[float] = []
    caps: list[int] = []
    for e in enrollments:
        pool = by_course[int(e["course_id"])]  # type: ignore[call-overload]
        start_day = float(e["start_day"])  # type: ignore[arg-type]
        due = sum(
            1 for a in pool
            if start_day + float(a["due_offset_days"]) <= HORIZON_DAYS  # type: ignore[arg-type]
        )
        weights.append(due / len(pool) if pool else 0.0)
        caps.append(due * 50)
    counts = submission_counts(rng, len(enrollments), target, caps=caps, weights=weights)
    n_weeks = HORIZON_DAYS // 7 + 3
    handles = [(workdir / f"w{w:04d}.tsv").open("w", encoding="utf-8") for w in range(n_weeks)]
    written = 0
    sub_id = 0
    try:
        for enrollment, n in zip(enrollments, counts, strict=True):
            if n == 0:
                continue
            start_day = float(enrollment["start_day"])  # type: ignore[arg-type]
            # Only assignments whose due date has already passed can have submissions.
            course_assignments = [
                a
                for a in by_course[int(enrollment["course_id"])]  # type: ignore[call-overload]
                if start_day + float(a["due_offset_days"]) <= HORIZON_DAYS  # type: ignore[arg-type]
            ]
            k = len(course_assignments)
            if k == 0:
                continue
            weights = [float(a["retry_weight"]) for a in course_assignments]  # type: ignore[arg-type]
            picks = Counter(rng.choices(range(k), weights=weights, k=n))
            picks = redistribute(picks, k)
            weeks = int(enrollment["weeks"])  # type: ignore[call-overload]
            for idx, attempts in picks.items():
                assignment = course_assignments[idx]
                due_day = start_day + float(assignment["due_offset_days"])  # type: ignore[arg-type]
                for attempt in range(1, attempts + 1):
                    # Submissions cluster just before the deadline and retries follow
                    # within hours -- the shape that makes weekly time-series queries
                    # interesting rather than flat.
                    day = due_day - abs(rng.gauss(0, 2.2)) + (attempt - 1) * rng.uniform(0.01, 0.5)
                    day = min(max(day, start_day - 1.0), start_day + weeks * 7 + 21)
                    # Clamp rather than skip: dropping out-of-range rows would make the
                    # loaded total drift below the requested one and, worse, would drop
                    # them non-uniformly (only from cohorts near the edges of the window).
                    day = min(max(day, 0.0), float(HORIZON_DAYS) - 0.001)
                    r = rng.random()
                    if r < 0.80:
                        status, graded = "graded", True
                    elif r < 0.90:
                        status, graded = "late", rng.random() < 0.7
                    elif r < 0.995:
                        status, graded = "submitted", False
                    else:
                        status, graded = "plagiarised", False
                    max_score = int(assignment["max_score"])  # type: ignore[call-overload]
                    if graded:
                        # Later attempts score better: people learn.
                        bonus = min(18.0, 5.0 * math.log1p(attempt - 1) * 2)
                        pct = min(1.0, max(0.0, rng.gauss(0.72, 0.18) + bonus / 100.0))
                        score = round(pct * max_score, 2)
                    else:
                        score = None
                    sub_id += 1
                    kind = str(assignment["kind"])
                    runtime = (
                        str(int(abs(rng.gauss(400, 900)) + 5))
                        if kind in ("coding", "project") else r"\N"
                    )
                    line = "\t".join(
                        [
                            str(sub_id),
                            str(enrollment["id"]),
                            str(assignment["id"]),
                            str(attempt),
                            "graded" if graded else status,
                            r"\N" if score is None else str(score),
                            ts(day),
                            ts(day + rng.uniform(0.2, 6.0)) if graded else r"\N",
                            runtime,
                        ]
                    )
                    handles[int(day // 7)].write(line + "\n")
                    written += 1
    finally:
        for h in handles:
            h.close()
    return written, sub_id


def load_submission_buckets(conn: psycopg.Connection[object], workdir: pathlib.Path) -> int:
    cols = (
        "id, enrollment_id, assignment_id, attempt_no, status, score, "
        "submitted_at, graded_at, runtime_ms"
    )
    total = 0
    with conn.cursor() as cur:
        for path in sorted(workdir.glob("w*.tsv")):
            size = path.stat().st_size
            if size == 0:
                continue
            with path.open("r", encoding="utf-8") as fh, cur.copy(
                f"COPY submissions ({cols}) FROM STDIN"
            ) as copy:
                while chunk := fh.read(1 << 20):
                    copy.write(chunk)
                    total += chunk.count("\n")
    return total


def gen_mentor_sessions(
    rng: random.Random, enrollments: list[dict[str, object]], mentor_ids: list[int]
) -> Iterator[list[str]]:
    sid = 0
    for e in enrollments:
        r = rng.random()
        n = 0 if r < 0.38 else 1 if r < 0.62 else 2 if r < 0.80 else 3 if r < 0.91 else (
            rng.randint(4, 12)
        )
        start_day = float(e["start_day"])  # type: ignore[arg-type]
        weeks = int(e["weeks"])  # type: ignore[call-overload]
        for i in range(n):
            sid += 1
            day = start_day + (i + 1) * (weeks * 7) / (n + 1) + rng.uniform(-2, 2)
            if day < 0 or day >= HORIZON_DAYS:
                continue
            p = rng.random()
            status = (
                "completed" if p < 0.78 else "no_show" if p < 0.86
                else "cancelled" if p < 0.94 else "scheduled"
            )
            rating = (
                str(min(5, max(1, int(rng.gauss(4.2, 0.9)))))
                if status == "completed" and rng.random() < 0.72 else r"\N"
            )
            yield [
                str(sid),
                str(e["id"]),
                str(mentor_ids[rng.randrange(len(mentor_ids))]),
                ts(day),
                str(rng.choice([15, 30, 30, 45, 60, 60, 90])),
                status,
                rating,
                r"\N",
            ]


def gen_payments(
    rng: random.Random,
    enrollments: list[dict[str, object]],
    courses: list[dict[str, object]],
    n_learners: int,
) -> Iterator[list[str]]:
    price = {int(c["id"]): int(c["list_price_cents"]) for c in courses}  # type: ignore[call-overload]
    pid = 0
    for e in enrollments:
        if rng.random() > 0.82:
            continue  # scholarships, employer deals invoiced elsewhere, free trials
        pid += 1
        gross = price[int(e["course_id"])]  # type: ignore[call-overload]
        if rng.random() < 0.22:
            gross = int(gross * rng.choice([0.5, 0.7, 0.8]))  # discount codes
        r = rng.random()
        status = (
            "succeeded" if r < 0.86 else "refunded" if r < 0.92
            else "failed" if r < 0.975 else "pending"
        )
        created = float(e["enrolled_at"]) - rng.uniform(0, 2)  # type: ignore[arg-type]
        created = max(0.0, created)
        refunded = int(gross * rng.choice([0.25, 0.5, 1.0, 1.0])) if status == "refunded" else 0
        settled = (
            ts(created + rng.uniform(0.001, 3.0)) if status in ("succeeded", "refunded")
            else r"\N"
        )
        yield [
            str(pid),
            str(e["learner_id"]),
            str(e["id"]),
            str(gross),
            "USD",
            status,
            weighted_pick(
                rng,
                [("card", 0.66), ("paypal", 0.16), ("bank_transfer", 0.11), ("employer_invoice", 0.07)],
            ),
            ts(created),
            settled,
            str(refunded),
            f"ch_{pid:09d}",
        ]
    # Subscription payments that belong to no single enrollment: this is why
    # payments.enrollment_id is NULLable.
    for _ in range(int(n_learners * 0.09)):
        pid += 1
        created = rng.uniform(0, HORIZON_DAYS)
        yield [
            str(pid),
            str(rng.randint(1, n_learners)),
            r"\N",
            str(rng.choice([1900, 2900, 4900])),
            "USD",
            "succeeded",
            "card",
            ts(created),
            ts(created + rng.uniform(0.001, 1.0)),
            "0",
            f"ch_{pid:09d}",
        ]


SEQUENCES = [
    ("learners", "id"), ("courses", "id"), ("cohorts", "id"), ("enrollments", "id"),
    ("assignments", "id"), ("submissions", "id"), ("mentor_sessions", "id"),
    ("payments", "id"),
]


def resync_sequences(conn: psycopg.Connection[object]) -> None:
    """COPY with explicit ids does not advance the identity sequence.

    Skip this and the next plain INSERT fails with a duplicate key error on a table that
    looks perfectly healthy -- a genuinely confusing bug the first time you meet it.
    """
    with conn.cursor() as cur:
        for table, col in SEQUENCES:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
                f"COALESCE((SELECT MAX({col}) FROM {table}), 1), true)"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the learning platform database.")
    ap.add_argument(
        "--scale", type=float, default=1.0,
        help="1.0 = 100k learners / 5M submissions. 0.01 gives a fast local dataset.",
    )
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--keep-going", action="store_true", help="do not truncate first")
    args = ap.parse_args()

    settings = get_settings()
    dsn = args.dsn or settings.admin_dsn
    rng = random.Random(SEED)

    n_learners = max(50, int(100_000 * args.scale))
    n_submissions = max(500, int(5_000_000 * args.scale))
    n_courses = 40 if args.scale >= 0.05 else 8
    cohorts_per_course = 16 if args.scale >= 0.05 else 4

    t0 = time.perf_counter()
    print(f"seeding {dsn}")
    print(f"  target: {n_learners:,} learners, {n_submissions:,} submissions")

    courses = gen_courses(rng, n_courses)
    cohorts = gen_cohorts(rng, courses, cohorts_per_course)
    assignments = gen_assignments(rng, courses)

    with psycopg.connect(dsn, autocommit=False) as conn:
        loader = Loader(conn)
        if not args.keep_going:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE payments, mentor_sessions, submissions, enrollments, "
                    "assignments, cohorts, courses, learners RESTART IDENTITY CASCADE"
                )
            conn.commit()

        step = time.perf_counter()
        n = loader.load(
            "learners",
            ["id", "email", "full_name", "country_code", "timezone", "status",
             "is_mentor", "signup_ts", "last_active_ts"],
            gen_learner_rows(rng, n_learners),
        )
        conn.commit()
        print(f"  learners        {n:>10,}  ({time.perf_counter() - step:.1f}s)")

        step = time.perf_counter()
        loader.load(
            "courses",
            ["id", "slug", "title", "subject", "difficulty", "list_price_cents",
             "duration_weeks", "is_active", "published_at"],
            (
                [str(c["id"]), esc(str(c["slug"])), esc(str(c["title"])), esc(str(c["subject"])),
                 str(c["difficulty"]), str(c["list_price_cents"]), str(c["duration_weeks"]),
                 "t" if c["is_active"] else "f", str(c["published_at"])]
                for c in courses
            ),
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT id FROM learners WHERE is_mentor ORDER BY id")
            mentor_ids = [int(r[0]) for r in cur.fetchall()]
        if not mentor_ids:
            mentor_ids = [1]

        # Cohort mentors must exist before cohorts are inserted (FK to learners).
        loader.load(
            "cohorts",
            ["id", "course_id", "name", "starts_on", "ends_on", "capacity", "status",
             "mentor_id"],
            (
                [str(c["id"]), str(c["course_id"]), esc(str(c["name"])), str(c["starts_on"]),
                 str(c["ends_on"]), str(c["capacity"]), str(c["status"]),
                 str(mentor_ids[rng.randrange(len(mentor_ids))])]
                for c in cohorts
            ),
        )
        loader.load(
            "assignments",
            ["id", "course_id", "position", "title", "kind", "max_score",
             "due_offset_days", "is_optional"],
            (
                [str(a["id"]), str(a["course_id"]), str(a["position"]), esc(str(a["title"])),
                 str(a["kind"]), str(a["max_score"]), str(a["due_offset_days"]),
                 "t" if a["is_optional"] else "f"]
                for a in assignments
            ),
        )
        conn.commit()
        print(f"  courses/cohorts/assignments  {len(courses)}/{len(cohorts)}/{len(assignments)}"
              f"  ({time.perf_counter() - step:.1f}s)")

        step = time.perf_counter()
        enrollments = gen_enrollments(rng, n_learners, cohorts, mentor_ids)
        n = loader.load(
            "enrollments",
            ["id", "learner_id", "cohort_id", "course_id", "status", "enrolled_at",
             "completed_at", "final_grade"],
            enrollment_rows(enrollments),
        )
        conn.commit()
        print(f"  enrollments     {n:>10,}  ({time.perf_counter() - step:.1f}s)")

        step = time.perf_counter()
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="nl2sql-seed-"))
        try:
            generated, _ = write_submission_buckets(
                rng, enrollments, assignments, n_submissions, workdir
            )
            print(f"  submissions generated {generated:>10,}  ({time.perf_counter() - step:.1f}s)")
            step = time.perf_counter()
            loaded = load_submission_buckets(conn, workdir)
            conn.commit()
            print(f"  submissions loaded    {loaded:>10,}  ({time.perf_counter() - step:.1f}s)")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        step = time.perf_counter()
        n = loader.load(
            "mentor_sessions",
            ["id", "enrollment_id", "mentor_id", "scheduled_at", "duration_min", "status",
             "learner_rating", "notes"],
            gen_mentor_sessions(rng, enrollments, mentor_ids),
        )
        conn.commit()
        print(f"  mentor_sessions {n:>10,}  ({time.perf_counter() - step:.1f}s)")

        step = time.perf_counter()
        n = loader.load(
            "payments",
            ["id", "learner_id", "enrollment_id", "amount_cents", "currency", "status",
             "method", "created_at", "settled_at", "refunded_amount_cents", "external_ref"],
            gen_payments(rng, enrollments, courses, n_learners),
        )
        conn.commit()
        print(f"  payments        {n:>10,}  ({time.perf_counter() - step:.1f}s)")

        resync_sequences(conn)
        conn.commit()

    # ANALYZE outside the loading transaction. Without fresh statistics the planner is
    # working from the defaults it assumed when the tables were empty, and every plan you
    # look at afterwards is a plan for a database that does not exist.
    step = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("VACUUM ANALYZE")
    print(f"  vacuum analyze  ({time.perf_counter() - step:.1f}s)")
    print(f"seed complete in {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
