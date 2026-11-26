"""Deterministic seed for the e-commerce database: exactly 50,000 rows over 7 tables.

    categories 20 | customers 2,000 | products 500 | orders 10,000
    order_items 25,000 | payments 10,000 | reviews 2,480      -> 50,000

Determinism: one `random.Random(SEED)`, a fixed date anchor (never `now()`), and COPY in
primary-key order. Two runs produce byte-identical tables (`--checksum` prints a digest
so that can be verified rather than asserted).

    python db/seed_ecommerce.py [--dsn ...] [--checksum]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import random
from decimal import Decimal

import psycopg

SEED = 20240607
ANCHOR = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)  # "today" for the dataset
N_CUSTOMERS, N_PRODUCTS, N_ORDERS, N_ITEMS, N_REVIEWS = 2_000, 500, 10_000, 25_000, 2_480

DEFAULT_DSN = os.environ.get(
    "NL2SQL_ADMIN_DSN", "postgresql://nl2sql:nl2sql_admin_dev@localhost:5433/nl2sql_shop"
)
TABLES = ["categories", "customers", "products", "orders", "order_items", "payments", "reviews"]

TOP = ["Electronics", "Home & Kitchen", "Books", "Clothing", "Sports", "Toys", "Beauty", "Garden"]
SUB = {
    "Electronics": ["Phones", "Laptops"],
    "Home & Kitchen": ["Cookware", "Furniture"],
    "Books": ["Fiction", "Non-fiction"],
    "Clothing": ["Menswear", "Womenswear"],
    "Sports": ["Fitness"],
    "Toys": ["Puzzles"],
    "Beauty": ["Skincare"],
    "Garden": ["Tools"],
}
COUNTRIES = [("United States", 40), ("United Kingdom", 15), ("Germany", 12), ("France", 9),
             ("Canada", 8), ("Australia", 6), ("India", 6), ("Brazil", 4)]
CITIES = ["Springfield", "Riverton", "Lakeside", "Hillcrest", "Fairview", "Georgetown",
          "Madison", "Clinton", "Salem", "Franklin"]
FIRST = ["Ava", "Liam", "Noah", "Mia", "Zoe", "Ethan", "Leo", "Ivy", "Omar", "Priya",
         "Chen", "Sofia", "Lucas", "Nora", "Ravi", "Hana", "Mateo", "Elif", "Jon", "Amara"]
LAST = ["Smith", "Garcia", "Kim", "Patel", "Nguyen", "Brown", "Rossi", "Khan", "Silva",
        "Muller", "Dubois", "Lopez", "Ivanov", "Sato", "Okafor", "Cohen", "Berg", "Haas"]
ADJ = ["Pro", "Ultra", "Classic", "Eco", "Smart", "Compact", "Deluxe", "Essential"]
NOUN = ["Kettle", "Lamp", "Backpack", "Headset", "Notebook", "Blender", "Jacket", "Drone",
        "Mat", "Speaker", "Mug", "Chair", "Watch", "Camera", "Shoes", "Game"]
COUPONS = ["WELCOME10", "SPRING15", "VIP20", "FREESHIP", "BLACKFRI"]
TITLES = {5: ["Love it", "Perfect", "Excellent"], 4: ["Very good", "Solid"],
          3: ["It's okay", "Average"], 2: ["Disappointing", "Meh"], 1: ["Broke fast", "Awful"]}


def _wchoice(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _money(x: float) -> Decimal:
    return Decimal(f"{x:.2f}")


def build(rng: random.Random) -> dict[str, list[tuple]]:
    rows: dict[str, list[tuple]] = {t: [] for t in TABLES}

    # categories: 8 top-level (parent NULL) + 12 children = 20
    cid = 0
    parent_ids: dict[str, int] = {}
    for name in TOP:
        cid += 1
        parent_ids[name] = cid
        rows["categories"].append((cid, name, None))
    for name in TOP:
        for child in SUB[name]:
            cid += 1
            rows["categories"].append((cid, child, parent_ids[name]))
    child_ids = [r[0] for r in rows["categories"] if r[2] is not None]
    assert len(rows["categories"]) == 20

    for i in range(1, N_CUSTOMERS + 1):
        country = _wchoice(rng, COUNTRIES)
        signup = (ANCHOR - dt.timedelta(days=rng.randint(30, 1100))).date()
        first, last = rng.choice(FIRST), rng.choice(LAST)
        rows["customers"].append((
            i, f"{first.lower()}.{last.lower()}{i}@example.com", f"{first} {last}", country,
            rng.choice(CITIES),
            rng.choices(["consumer", "corporate", "small_business"], [70, 12, 18])[0],
            signup, rng.random() > 0.08, rng.random() < 0.45,
        ))
    signup_of = {r[0]: r[6] for r in rows["customers"]}

    price_of: dict[int, Decimal] = {}
    for i in range(1, N_PRODUCTS + 1):
        price = _money(rng.lognormvariate(3.4, 0.9) + 3)
        cost = _money(float(price) * rng.uniform(0.4, 0.8))
        price_of[i] = price
        rows["products"].append((
            i, f"SKU-{i:05d}", f"{rng.choice(ADJ)} {rng.choice(NOUN)} {i}",
            rng.choice(child_ids), price, cost, rng.randint(0, 500), rng.random() > 0.06,
            ANCHOR - dt.timedelta(days=rng.randint(60, 1200), minutes=rng.randint(0, 1439)),
        ))

    # order sizes: every order gets 2 items, plus 5,000 extra spread at random -> 25,000
    sizes = [2] * N_ORDERS
    for idx in rng.choices(range(N_ORDERS), k=N_ITEMS - 2 * N_ORDERS):
        sizes[idx] += 1
    assert sum(sizes) == N_ITEMS

    item_id = 0
    for oid in range(1, N_ORDERS + 1):
        cust = rng.randint(1, N_CUSTOMERS)
        earliest = max(signup_of[cust], (ANCHOR - dt.timedelta(days=720)).date())
        span = max((ANCHOR.date() - earliest).days, 1)
        odate = dt.datetime.combine(earliest, dt.time(), tzinfo=dt.UTC) + dt.timedelta(
            days=rng.randrange(span), seconds=rng.randrange(86_400))
        age_days = (ANCHOR - odate).days
        status = rng.choices(
            ["delivered", "shipped", "paid", "placed", "cancelled", "refunded"],
            [62, 8, 6, 4, 12, 8])[0]
        if age_days > 30 and status in ("placed", "paid", "shipped") and rng.random() < 0.8:
            status = "delivered"
        shipped = delivered = None
        if status in ("shipped", "delivered", "refunded"):
            shipped = odate + dt.timedelta(days=rng.randint(1, 3))
        if status in ("delivered", "refunded") and shipped is not None:
            delivered = shipped + dt.timedelta(days=rng.randint(2, 9))
            if delivered > ANCHOR:
                delivered = None
                status = "shipped" if status == "delivered" else status
        total = Decimal("0")
        for _ in range(sizes[oid - 1]):
            item_id += 1
            pid = rng.randint(1, N_PRODUCTS)
            qty = rng.choices([1, 2, 3, 4, 5], [55, 25, 10, 6, 4])[0]
            disc = Decimal(rng.choices(["0.00", "0.05", "0.10", "0.20", "0.50"],
                                       [70, 12, 10, 7, 1])[0])
            unit = price_of[pid]
            total += (unit * qty * (1 - disc)).quantize(Decimal("0.01"))
            rows["order_items"].append((item_id, oid, pid, qty, unit, disc))
        coupon = rng.choice(COUPONS) if rng.random() < 0.28 else None
        country = rows["customers"][cust - 1][3] if rng.random() < 0.9 else _wchoice(rng, COUNTRIES)
        rows["orders"].append((oid, cust, status, odate, country, total, coupon, shipped, delivered))

        # one payment per order
        if status == "placed":
            pstatus = "pending"
        elif status == "cancelled":
            pstatus = "failed" if rng.random() < 0.7 else "refunded"
        elif status == "refunded":
            pstatus = "refunded"
        else:
            pstatus = "succeeded"
        paid_at = odate + dt.timedelta(minutes=rng.randint(1, 90)) if pstatus in (
            "succeeded", "refunded") else None
        rows["payments"].append((
            oid, oid, rng.choices(["card", "paypal", "bank_transfer", "gift_card"],
                                  [62, 22, 11, 5])[0], pstatus, total, paid_at))

    seen: set[tuple[int, int]] = set()
    rid = 0
    while rid < N_REVIEWS:
        pid, cust = rng.randint(1, N_PRODUCTS), rng.randint(1, N_CUSTOMERS)
        if (pid, cust) in seen:
            continue
        seen.add((pid, cust))
        rid += 1
        rating = rng.choices([1, 2, 3, 4, 5], [6, 7, 14, 30, 43])[0]
        body = None if rng.random() < 0.3 else f"{rng.choice(TITLES[rating])} - would {'' if rating >= 4 else 'not '}buy again."
        rows["reviews"].append((
            rid, pid, cust, rating, rng.choice(TITLES[rating]), body,
            ANCHOR - dt.timedelta(days=rng.randint(1, 700), minutes=rng.randint(0, 1439))))
    return rows


def load(dsn: str) -> dict[str, int]:
    rows = build(random.Random(SEED))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(reversed(TABLES))} RESTART IDENTITY CASCADE")
        for table in TABLES:
            with cur.copy(f"COPY {table} FROM STDIN") as copy:
                for row in rows[table]:
                    copy.write_row(row)
        # Defensive: guarantee the read-only role sees every table even if the schema was
        # created by someone else after init_roles.sql ran.
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO nl2sql_ro")
        conn.commit()
        conn.autocommit = True
        cur.execute("VACUUM ANALYZE")
        counts = {}
        for table in TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
    return counts


def checksum(dsn: str) -> str:
    h = hashlib.sha256()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SELECT * FROM {table} ORDER BY id")
            for row in cur.fetchall():
                h.update(repr(row).encode())
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--checksum", action="store_true", help="print a sha256 of all table contents")
    args = ap.parse_args()
    counts = load(args.dsn)
    for t, n in counts.items():
        print(f"{t:12s} {n:>7,}")
    print(f"{'TOTAL':12s} {sum(counts.values()):>7,}")
    if args.checksum:
        print("sha256", checksum(args.dsn))


if __name__ == "__main__":
    main()
