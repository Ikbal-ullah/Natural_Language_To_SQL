"""E-commerce evaluation: Execution Accuracy, Unsafe Rejection Rate, latency, 3 ablations.

    # live model (needs ANTHROPIC_API_KEY) -- the numbers that describe the system:
    ANTHROPIC_API_KEY=sk-ant-... .venv312/bin/python eval/run_eval.py --provider anthropic

    # deterministic harness verification (no LLM, no key, no cost):
    .venv312/bin/python eval/run_eval.py --provider oracle
    .venv312/bin/python eval/run_eval.py --provider faulty

ABLATIONS (cumulative)
  baseline          full schema in the prompt, 0 retries
  +schema_linking   BM25 + 1-hop-FK schema linking, 0 retries
  +error_retries    schema linking, up to 2 error-guided retries

METRICS (all computed here from real executions, nothing is estimated)
  Execution Accuracy   answerable questions whose result set equals the reference result
                       set when BOTH are executed on PostgreSQL (eval/comparator.py)
  Unsafe Rejection     adversarial unsafe items whose pipeline status is `rejected`
                       (a `security_violation` is counted separately: those get HTTP 403)
  Unanswerable handled unanswerable items that returned no rows (clarify / rejected / error)
  Latency              p50 / p95 of Pipeline.answer() wall time per question (nearest-rank).
                       With the oracle/faulty providers this contains NO LLM time.
  Linking recall       share of answerable questions whose linked tables include every table
                       the reference SQL uses (deterministic, no LLM)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

# Point at the docker database unless the caller already did (see .env.ecommerce).
for key, value in {
    "NL2SQL_ADMIN_DSN": "postgresql://nl2sql:nl2sql_admin_dev@localhost:5433/nl2sql_shop",
    "NL2SQL_READONLY_DSN": "postgresql://nl2sql_ro:nl2sql_ro_dev@localhost:5433/nl2sql_shop",
    "NL2SQL_DOMAIN": "ecommerce",
    "NL2SQL_STATEMENT_TIMEOUT_MS": "3000",
    "NL2SQL_LARGE_TABLE_ROWS": "20000",
    "NL2SQL_MAX_PLAN_COST": "100000",
}.items():
    os.environ.setdefault(key, value)

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from eval.comparator import compare  # noqa: E402
from eval.harness_providers import PROVIDERS  # noqa: E402
from nl2sql.config import Settings  # noqa: E402
from nl2sql.db import close_pools, fetch_all  # noqa: E402
from nl2sql.linker import link_schema  # noqa: E402
from nl2sql.pipeline import Pipeline  # noqa: E402
from nl2sql.schema import introspect  # noqa: E402

DATASET = ROOT / "eval" / "golden_dataset.json"
ARTIFACTS = ROOT / "eval" / "artifacts"

ABLATIONS: list[tuple[str, bool, int]] = [
    ("baseline", False, 0),
    ("+schema_linking", True, 0),
    ("+error_retries", True, 2),
]


@dataclass
class ItemResult:
    id: str
    difficulty: str
    expect: str
    status: str
    passed: bool
    security_violation: bool
    retries: int
    latency_ms: float
    tables_selected: int
    reason: str = ""
    sql: str | None = None


@dataclass
class AblationResult:
    name: str
    schema_linking: bool
    max_retries: int
    items: list[ItemResult] = field(default_factory=list)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def reference_tables(sql: str, catalog_tables: set[str]) -> set[str]:
    tree = sqlglot.parse_one(sql, dialect="postgres")
    ctes = {str(c.alias_or_name).lower() for c in tree.find_all(exp.CTE)}
    names = {str(t.name).lower() for t in tree.find_all(exp.Table)}
    return {n for n in names if n in catalog_tables and n not in ctes}


def score(item: dict[str, Any], answer: Any, ref_rows: list[tuple[Any, ...]] | None) -> tuple[bool, str]:
    expect = item["expect"]
    if expect == "answer":
        if answer.status != "ok":
            return False, f"status={answer.status}: {(answer.error or '')[:120]}"
        assert ref_rows is not None
        cmp = compare(ref_rows, answer.rows, order_matters=item["order_matters"])
        return cmp.match, cmp.reason
    if expect == "reject":
        return answer.status == "rejected", f"status={answer.status}"
    # unanswerable: success = no data was returned
    return answer.status != "ok", f"status={answer.status}"


def run_ablation(
    name: str, linking: bool, retries: int, items: list[dict[str, Any]],
    refs: dict[str, list[tuple[Any, ...]]], provider: Any, settings: Settings, catalog: Any,
) -> AblationResult:
    pipe = Pipeline(provider=provider, catalog=catalog, settings=settings)
    out = AblationResult(name, linking, retries)
    for it in items:
        t0 = time.perf_counter()
        ans = pipe.answer(it["question"], max_retries=retries, schema_linking=linking)
        wall = (time.perf_counter() - t0) * 1000
        ok, reason = score(it, ans, refs.get(it["id"]))
        out.items.append(ItemResult(
            it["id"], it["difficulty"], it["expect"], ans.status, ok, ans.security_violation,
            ans.retries, round(wall, 2), len(ans.tables_selected), reason, ans.sql,
        ))
    return out


def summarize(res: AblationResult) -> dict[str, Any]:
    answerable = [i for i in res.items if i.expect == "answer"]
    unsafe = [i for i in res.items if i.expect == "reject"]
    unans = [i for i in res.items if i.expect == "unanswerable"]
    by_diff: dict[str, list[ItemResult]] = defaultdict(list)
    for i in answerable:
        by_diff[i.difficulty].append(i)
    lat = [i.latency_ms for i in res.items]
    lat_ans = [i.latency_ms for i in answerable]
    return {
        "ablation": res.name,
        "schema_linking": res.schema_linking,
        "max_retries": res.max_retries,
        "execution_accuracy": {"correct": sum(i.passed for i in answerable), "total": len(answerable)},
        "execution_accuracy_by_difficulty": {
            d: {"correct": sum(i.passed for i in v), "total": len(v)} for d, v in sorted(by_diff.items())
        },
        "unsafe_rejection": {"rejected": sum(i.passed for i in unsafe), "total": len(unsafe),
                             "http_403_class": sum(i.security_violation for i in unsafe)},
        "unanswerable_handled": {"handled": sum(i.passed for i in unans), "total": len(unans)},
        "latency_ms_all": {"p50": round(percentile(lat, 50), 2), "p95": round(percentile(lat, 95), 2), "n": len(lat)},
        "latency_ms_answerable": {"p50": round(percentile(lat_ans, 50), 2), "p95": round(percentile(lat_ans, 95), 2)},
        "retries_used_total": sum(i.retries for i in res.items),
        "questions_that_used_a_retry": sum(1 for i in res.items if i.retries > 0),
        "mean_tables_in_prompt": round(sum(i.tables_selected for i in res.items) / len(res.items), 2),
        "failures": [{"id": i.id, "reason": i.reason} for i in res.items if not i.passed],
    }


def linking_recall(items: list[dict[str, Any]], catalog: Any, top_k: int, max_tables: int) -> dict[str, Any]:
    names = set(catalog.tables)
    hit = 0
    total = 0
    sizes = []
    misses = []
    for it in items:
        if it["expect"] != "answer":
            continue
        need = reference_tables(it["reference_sql"], names)
        got = set(link_schema(it["question"], catalog, top_k=top_k, max_tables=max_tables).selected)
        total += 1
        sizes.append(len(got))
        if need <= got:
            hit += 1
        else:
            misses.append({"id": it["id"], "missing": sorted(need - got)})
    return {"covered": hit, "total": total, "mean_tables_selected": round(sum(sizes) / total, 2),
            "schema_tables": len(names), "top_k": top_k, "max_tables": max_tables, "misses": misses}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--provider", choices=["anthropic", *PROVIDERS], required=True)
    ap.add_argument("--limit", type=int, default=0, help="only the first N items (smoke test)")
    args = ap.parse_args()

    settings = Settings()
    items = json.loads(DATASET.read_text())["items"]
    if args.limit:
        items = items[: args.limit]

    if args.provider == "anthropic":
        if not (settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
            print("ANTHROPIC_API_KEY is not set; refusing to fall back to an offline baseline "
                  "and call it a model result. Use --provider oracle|faulty for harness checks.")
            return 2
        from nl2sql.llm import AnthropicProvider
        provider: Any = AnthropicProvider(settings)
    else:
        provider = PROVIDERS[args.provider](items)

    catalog = introspect(settings)
    refs = {}
    for it in items:
        if it["expect"] == "answer":
            refs[it["id"]] = fetch_all(it["reference_sql"], settings=settings)[1]

    results = [run_ablation(n, lk, r, items, refs, provider, settings, catalog) for n, lk, r in ABLATIONS]
    summary = {
        "provider": args.provider,
        "model": getattr(provider, "name", args.provider) if args.provider != "anthropic" else settings.model,
        "is_llm": args.provider == "anthropic",
        "n_items": len(items),
        "dataset": "eval/golden_dataset.json",
        "ablations": [summarize(r) for r in results],
        "schema_linking_recall": linking_recall(items, catalog, settings.link_top_k, settings.link_max_tables),
        "schema_linking_sweep": [
            linking_recall(items, catalog, k, m) for k, m in [(1, 3), (2, 4), (2, 5), (3, 5), (3, 7)]
        ],
        "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ARTIFACTS.mkdir(exist_ok=True)
    out = ARTIFACTS / f"ecom_{args.provider}.json"
    out.write_text(json.dumps(summary, indent=1) + "\n")

    print(f"provider={args.provider} items={len(items)} -> {out.relative_to(ROOT)}")
    hdr = f"{'ablation':<18}{'EX':>9}{'unsafe rej':>12}{'unans':>8}{'p50 ms':>9}{'p95 ms':>9}{'retries':>9}"
    print(hdr)
    for a in summary["ablations"]:
        ex, ur, un = a["execution_accuracy"], a["unsafe_rejection"], a["unanswerable_handled"]
        print(f"{a['ablation']:<18}{ex['correct']:>4}/{ex['total']:<4}{ur['rejected']:>6}/{ur['total']:<5}"
              f"{un['handled']:>4}/{un['total']:<3}{a['latency_ms_all']['p50']:>9}{a['latency_ms_all']['p95']:>9}"
              f"{a['retries_used_total']:>9}")
    lr = summary["schema_linking_recall"]
    print(f"schema-linking recall: {lr['covered']}/{lr['total']} questions, "
          f"mean {lr['mean_tables_selected']} of {lr['schema_tables']} tables")
    close_pools()
    return 0


if __name__ == "__main__":
    sys.exit(main())
