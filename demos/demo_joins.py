"""Every join type on the same two five-row tables, with the actual rows printed.

Run: python demos/demo_joins.py
Tables are built inline with VALUES, so nothing is created and every row is checkable
by hand -- which is the point.
"""

import _bootstrap  # noqa: F401

from nl2sql.db import fetch_all

SETUP = """
WITH learners(id, name) AS (VALUES (1,'Ana'),(2,'Ben'),(3,'Chen'),(4,'Dara'),(5,'Eli')),
     enrollments(learner_id, course) AS (VALUES (2,'SQL'),(3,'Rust'),(3,'Go'),(6,'C++'),(7,'UX'))
"""

ON = "ON e.learner_id = l.id"
JOINS: dict[str, tuple[str, str]] = {
    "INNER JOIN": ("Only pairs matching on both sides. Ben(1) + Chen(2) = 3.", f"JOIN enrollments e {ON}"),
    "LEFT JOIN": ("Every learner. Ana/Dara/Eli get NULL courses = 6 rows.", f"LEFT JOIN enrollments e {ON}"),
    "RIGHT JOIN": ("Every enrollment. Orphans 6 and 7 get NULL names.", f"RIGHT JOIN enrollments e {ON}"),
    "FULL JOIN": ("Both sides. 3 matched + 3 lonely learners + 2 orphans.", f"FULL JOIN enrollments e {ON}"),
    "CROSS JOIN": ("Every combination, no condition. 5 x 5 = 25 rows.", "CROSS JOIN enrollments e"),
}

def show(title: str, explanation: str, from_clause: str) -> None:
    sql = (
        f"{SETUP} SELECT l.id, l.name, e.learner_id, e.course "
        f"FROM learners l {from_clause} ORDER BY 1 NULLS LAST, 3 NULLS LAST, 4 LIMIT 30"
    )
    _, rows = fetch_all(sql)
    print(f"\n{'=' * 74}\n{title}  ->  {len(rows)} rows\n{explanation}\n{'=' * 74}")
    print(f"  {'l.id':>5} {'l.name':<8} {'e.learner_id':>13} {'e.course':<8}")
    for r in rows[:12]:
        c = ["NULL" if v is None else str(v) for v in r]
        print(f"  {c[0]:>5} {c[1]:<8} {c[2]:>13} {c[3]:<8}")
    print(f"  ... {len(rows) - 12} more" if len(rows) > 12 else "", end="")

def main() -> int:
    print("learners:    id 1..5 (Ana, Ben, Chen, Dara, Eli)")
    print("enrollments: learner_id 2, 3, 3, 6, 7  <- note 3 appears twice, 6 and 7 are orphans")
    for title, (explanation, from_clause) in JOINS.items():
        show(title, explanation, from_clause)
    show("ANTI JOIN (LEFT JOIN ... WHERE right IS NULL)",
         "Learners with no enrollment at all -- the idiom for 'rows with no match'.",
         f"LEFT JOIN enrollments e {ON} AND 1 = 1 WHERE e.learner_id IS NULL")
    print("\n\nChen appears TWICE in the inner join: a join multiplies rows, producing one")
    print("output row per matching PAIR. That is why count(*) after a join can exceed the")
    print("number of learners, and why SUM over a joined table silently double-counts.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
