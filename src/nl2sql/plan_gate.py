"""The query plan gate: refuse expensive queries before executing them.

THE PROBLEM THIS SOLVES
-----------------------
Validation proves a query is a legal, read-only question about tables that exist. It says
nothing about whether answering it will take 4 milliseconds or 40 seconds. Those are
different properties and they need different machinery:

    validator  -> "is this allowed?"      (static, cheap, certain)
    plan gate  -> "can we afford this?"   (asks the planner, cheap, estimated)
    timeout    -> "we were wrong"         (dynamic, the backstop)

The gate exists because a statement timeout alone is a bad way to enforce cost. A timeout
is a fuse that blows *after* the damage: five seconds of a 5M-row sequential scan has
already evicted the buffer cache that every other query on the box depends on, and under
concurrency you get a queue of doomed queries all doing that at once. Refusing beforehand
costs about a millisecond of planning and no I/O at all.

WHAT THE GATE CAN AND CANNOT KNOW
---------------------------------
EXPLAIN without ANALYZE does not run the query. It returns the plan the planner INTENDS
to use, with cost and row estimates derived from table statistics. That is exactly what we
want -- but it means the gate inherits every weakness of those estimates. A correlated
predicate ("learners in Germany who use the Berlin timezone") makes the planner multiply
two selectivities it believes are independent, and it can underestimate by 100x. So the
gate is a filter, not a proof, and the statement timeout stays in place underneath it.
Cost units are arbitrary (1.0 = one sequential page fetch), so the threshold is calibrated
against measurements on this dataset, not chosen from theory -- see BENCHMARKS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from nl2sql.config import Settings, get_settings
from nl2sql.db import Plan, PlanNode, explain, table_row_estimates

SCAN_NODES = frozenset({"Seq Scan", "Parallel Seq Scan"})
# A filtered scan that keeps at most this fraction of a large table is "selective".
SELECTIVE_FRACTION = 0.05


class Verdict(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class PlanFinding:
    code: str
    message: str
    verdict: Verdict


@dataclass
class GateResult:
    verdict: Verdict
    plan: Plan
    findings: list[PlanFinding] = field(default_factory=list)
    summary: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def reasons(self) -> str:
        return "; ".join(f.message for f in self.findings) or "plan within budget"


def _describe_node(node: PlanNode, large: set[str]) -> str:
    """One plain-English line per plan node, for the answer shown to a non-expert."""
    rel = node.relation
    rows = f"~{node.plan_rows:,.0f} rows"
    kind = node.node_type
    if kind in SCAN_NODES and rel:
        size = " (large table)" if rel in large else ""
        return f"reads every row of {rel}{size}, expecting {rows}"
    if kind == "Index Only Scan" and rel:
        return f"answers from the index on {rel} without touching the table ({rows})"
    if kind in ("Index Scan", "Bitmap Heap Scan") and rel:
        return f"looks up matching rows in {rel} via an index ({rows})"
    if kind == "Hash Join":
        return f"joins by building a hash table in memory ({rows} out)"
    if kind == "Nested Loop":
        return f"joins by probing the inner side once per outer row ({rows} out)"
    if kind == "Merge Join":
        return f"joins two already-sorted inputs ({rows} out)"
    if kind in ("Aggregate", "HashAggregate", "GroupAggregate"):
        return f"groups and aggregates ({rows} out)"
    if kind == "Sort":
        return f"sorts {rows}"
    if kind == "WindowAgg":
        return f"computes window functions over {rows}"
    if kind == "Limit":
        return f"stops after {rows}"
    return f"{kind} ({rows})"


def summarize(plan: Plan, large: set[str]) -> str:
    """Render the plan as a short, ordered, jargon-light description.

    Shown to the user next to the SQL. The point is not to teach them EXPLAIN; it is to
    let them notice 'reads every row of submissions' and realise the question was
    misunderstood before they trust the number.
    """
    seen: list[str] = []
    for node in plan.nodes:
        line = _describe_node(node, large)
        if line not in seen:
            seen.append(line)
    head = seen[:6]
    body = "; then ".join(head)
    return f"The database {body}. Estimated cost {plan.total_cost:,.0f}."


def check_plan(
    sql: str,
    *,
    settings: Settings | None = None,
    large_tables: set[str] | None = None,
    row_estimates: dict[str, int] | None = None,
) -> GateResult:
    """EXPLAIN the query and decide whether it may run."""
    settings = settings or get_settings()
    if large_tables is None:
        row_estimates = table_row_estimates(settings)
        large_tables = {
            name for name, rows in row_estimates.items() if rows >= settings.large_table_rows
        }
    row_estimates = row_estimates or {}

    plan = explain(sql, analyze=False, settings=settings)
    findings: list[PlanFinding] = []

    if plan.total_cost > settings.max_plan_cost:
        findings.append(
            PlanFinding(
                "COST_EXCEEDED",
                f"estimated cost {plan.total_cost:,.0f} exceeds the budget of "
                f"{settings.max_plan_cost:,.0f}",
                Verdict.BLOCK,
            )
        )

    for node in plan.nodes:
        if node.node_type in SCAN_NODES and node.relation in large_tables:
            table_rows = row_estimates.get(node.relation or "", 0)
            if (
                "Filter" in node.raw
                and table_rows > 0
                and node.plan_rows <= SELECTIVE_FRACTION * table_rows
            ):
                # A sequential scan that reads every row of a large table in order to keep
                # a small fraction of them is the plan-level signature of a missing index:
                # the predicate is selective but nothing can seek on it. Refused (the model
                # is told to filter on an indexed column); a plain unfiltered scan, which
                # an aggregate legitimately needs, stays a warning below.
                findings.append(
                    PlanFinding(
                        "SELECTIVE_SEQ_SCAN_LARGE_TABLE",
                        f"sequential scan over large table {node.relation} "
                        f"(~{table_rows:,} rows) filtered down to "
                        f"~{node.plan_rows:,.0f} on an unindexed column",
                        Verdict.BLOCK,
                    )
                )
                continue
            # A warning, not a block. Some legitimate questions genuinely require reading
            # every row -- "how many submissions are there in total" is one -- and a gate
            # that refuses those is a gate people route around. The cost check above is
            # what stops the ruinous cases; this finding is here so the user is told, in
            # words, that their question was answered the expensive way.
            findings.append(
                PlanFinding(
                    "FULL_SCAN_LARGE_TABLE",
                    f"sequential scan over large table {node.relation} "
                    f"(~{node.plan_rows:,.0f} rows estimated)",
                    Verdict.WARN,
                )
            )

    root_rows = plan.plan_rows
    if root_rows > settings.max_limit * 50:
        findings.append(
            PlanFinding(
                "WIDE_RESULT",
                f"plan expects to produce ~{root_rows:,.0f} rows before the LIMIT discards "
                "most of them",
                Verdict.WARN,
            )
        )

    for node in plan.nodes:
        if node.node_type == "Nested Loop" and node.plan_rows > settings.large_table_rows:
            # A nested loop whose outer side is huge is the plan-level signature of the
            # N+1 problem: one inner lookup per outer row. Cheap when the outer side is
            # 10 rows, quadratic-feeling when it is a million.
            findings.append(
                PlanFinding(
                    "LARGE_NESTED_LOOP",
                    f"nested loop join producing ~{node.plan_rows:,.0f} rows -- one inner "
                    "lookup per outer row",
                    Verdict.WARN,
                )
            )
            break

    verdict = Verdict.ALLOW
    if any(f.verdict is Verdict.BLOCK for f in findings):
        verdict = Verdict.BLOCK
    elif findings:
        verdict = Verdict.WARN

    return GateResult(
        verdict=verdict,
        plan=plan,
        findings=findings,
        summary=summarize(plan, large_tables),
    )
