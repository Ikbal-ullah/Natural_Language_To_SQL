"""Run the golden set and the adversarial set, and report per-category results.

LEGACY: evaluates the original 5M-row learners database. The e-commerce eval is eval/run_eval.py.

Two numbers matter and they are reported separately, never averaged together:

  EXECUTION ACCURACY, per category. One average across sixty questions hides the shape of
  the failure. A system at 72% might be 95% on filters and 20% on window functions, which
  tells you what to fix; the 72% tells you nothing you can act on. The ambiguous category
  is scored inverted -- a pass means the system asked a clarifying question instead of
  guessing.

  SAFETY BLOCK RATE, measured at the validator with the model removed from the loop. See
  eval/adversarial.py for why the pipeline-level number is reported as context rather than
  as the claim.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from eval.adversarial import ADVERSARIAL_CASES  # noqa: E402
from eval.comparator import compare  # noqa: E402
from eval.golden_set import CATEGORIES, GOLDEN_SET, GoldenQuestion  # noqa: E402
from nl2sql.config import get_settings  # noqa: E402
from nl2sql.db import fetch_all  # noqa: E402
from nl2sql.pipeline import Pipeline  # noqa: E402
from nl2sql.schema import get_catalog  # noqa: E402
from nl2sql.validator import validate  # noqa: E402

ARTIFACT_DIR = ROOT / "eval" / "artifacts"

# Published per-million-token prices for the configured model, used for the cost table.
# Kept here rather than inline so a price change is one edit.
PRICE_PER_MTOK = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_DISCOUNT = 0.1  # cached input tokens bill at about a tenth of the input rate


@dataclass
class QuestionResult:
    id: str
    category: str
    question: str
    status: str
    correct: bool
    reason: str
    sql: str | None = None
    reference_sql: str | None = None
    interpretation: str = ""
    clarification: str | None = None
    attempts: int = 0
    retried: bool = False
    latency_ms: float = 0.0
    generation_ms: float = 0.0
    execution_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def estimate_cost(model: str, in_tok: int, out_tok: int, cached_tok: int) -> float:
    """Dollar cost of one question. Arithmetic shown in LEARN.md's cost table."""
    price_in, price_out = PRICE_PER_MTOK.get(model, (5.00, 25.00))
    uncached = max(0, in_tok)
    return (
        uncached / 1_000_000 * price_in
        + cached_tok / 1_000_000 * price_in * CACHE_READ_DISCOUNT
        + out_tok / 1_000_000 * price_out
    )


def score_question(pipeline: Pipeline, q: GoldenQuestion) -> QuestionResult:
    """Run one question and decide whether the system got it right."""
    answer = pipeline.answer(q.question)
    base = QuestionResult(
        id=q.id,
        category=q.category,
        question=q.question,
        status=answer.status,
        correct=False,
        reason="",
        sql=answer.sql,
        reference_sql=q.sql,
        interpretation=answer.interpretation,
        clarification=answer.clarification_question,
        attempts=len(answer.attempts),
        retried=len(answer.attempts) > 1,
        latency_ms=answer.elapsed_ms,
        generation_ms=answer.generation_ms,
        execution_ms=answer.execution_ms,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
        cache_read_tokens=answer.cache_read_tokens,
        warnings=answer.warnings,
        error=answer.error,
    )

    if q.expects_clarification:
        # Inverted scoring. The right answer to an ambiguous question is a question.
        base.correct = answer.status == "clarify"
        base.reason = (
            "asked for clarification, as required"
            if base.correct
            else f"answered instead of asking (status={answer.status})"
        )
        return base

    if answer.status != "ok":
        base.reason = f"pipeline returned {answer.status}: {answer.error or 'no rows'}"
        return base

    assert q.sql is not None
    try:
        _, reference_rows = fetch_all(q.sql)
    except Exception as err:  # pragma: no cover - a broken reference is a repo bug
        base.reason = f"REFERENCE QUERY FAILED ({err}) -- fix the evaluation set"
        return base

    result = compare(reference_rows, answer.rows, order_matters=q.order_matters)
    base.correct = result.match
    base.reason = result.reason
    return base


def run_golden(pipeline: Pipeline, limit: int | None = None) -> list[QuestionResult]:
    questions = list(GOLDEN_SET)[:limit] if limit else list(GOLDEN_SET)
    results = []
    for i, q in enumerate(questions, 1):
        result = score_question(pipeline, q)
        results.append(result)
        mark = "PASS" if result.correct else "FAIL"
        print(f"  [{i:2}/{len(questions)}] {mark} {q.id} ({q.category}) {q.question[:52]}")
        if not result.correct:
            print(f"           -> {result.reason[:140]}")
    return results


def run_adversarial(pipeline: Pipeline, *, with_pipeline: bool) -> dict[str, Any]:
    """Measure the validator directly, and optionally the whole pipeline as context."""
    catalog = get_catalog()
    settings = get_settings()
    rows = []
    for case in ADVERSARIAL_CASES:
        verdict = validate(case.payload_sql, catalog, settings)
        codes = sorted({i.code.value for i in verdict.errors})
        entry: dict[str, Any] = {
            "id": case.id,
            "technique": case.technique,
            "prompt": case.prompt,
            "payload_sql": case.payload_sql,
            "policy_only": case.policy_only,
            "validator_blocked": not verdict.ok,
            "codes": codes,
            "expected_codes": list(case.expected_codes),
            "expected_check_fired": bool(set(codes) & set(case.expected_codes)),
        }
        if with_pipeline:
            answer = pipeline.answer(case.prompt)
            entry["pipeline_status"] = answer.status
            entry["pipeline_sql"] = answer.sql
            entry["pipeline_safe"] = answer.status in ("rejected", "clarify", "error") or (
                answer.sql is not None and validate(answer.sql, catalog, settings).ok
            )
        rows.append(entry)
        print(
            f"  {'BLOCK' if entry['validator_blocked'] else 'LEAK '} {case.id} "
            f"{case.technique:24} {','.join(codes) or '-'}"
        )
    blocked = sum(1 for r in rows if r["validator_blocked"])
    fired = sum(1 for r in rows if r["expected_check_fired"])
    return {
        "cases": rows,
        "total": len(rows),
        "validator_blocked": blocked,
        "block_rate": blocked / len(rows),
        "expected_check_fired": fired,
    }


def summarize(results: list[QuestionResult]) -> dict[str, Any]:
    """Per-category breakdown. One average across sixty questions hides the shape of the
    failure; 95% on filters and 20% on window functions is actionable, 72% is not."""
    by_cat: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        subset = [r for r in results if r.category == category]
        if not subset:
            continue
        passed = sum(1 for r in subset if r.correct)
        by_cat[category] = {
            "total": len(subset),
            "passed": passed,
            "accuracy": passed / len(subset),
            "failures": [
                {"id": r.id, "reason": r.reason[:200], "status": r.status}
                for r in subset
                if not r.correct
            ],
        }

    latencies = sorted(r.latency_ms for r in results if r.latency_ms > 0)
    exec_latencies = sorted(r.execution_ms for r in results if r.execution_ms > 0)

    def pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        k = max(0, min(len(values) - 1, round((len(values) - 1) * p)))
        return values[k]

    passed = sum(1 for r in results if r.correct)
    return {
        "overall": {
            "total": len(results),
            "passed": passed,
            "accuracy": passed / len(results) if results else 0.0,
        },
        "by_category": by_cat,
        "latency_ms": {
            "end_to_end_p50": round(statistics.median(latencies), 1) if latencies else 0.0,
            "end_to_end_p95": round(pct(latencies, 0.95), 1),
            "query_execution_p50": round(statistics.median(exec_latencies), 2)
            if exec_latencies
            else 0.0,
            "query_execution_p95": round(pct(exec_latencies, 0.95), 2),
        },
        "retries": {
            "questions_that_retried": sum(1 for r in results if r.retried),
            "total_attempts": sum(r.attempts for r in results),
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    lines = [
        "| category | passed | total | accuracy |",
        "| --- | --- | --- | --- |",
    ]
    for cat, data in s["by_category"].items():
        lines.append(
            f"| {cat} | {data['passed']} | {data['total']} | {data['accuracy'] * 100:.0f}% |"
        )
    o = s["overall"]
    lines.append(f"| **overall** | **{o['passed']}** | **{o['total']}** | "
                 f"**{o['accuracy'] * 100:.0f}%** |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="run only the first N questions")
    ap.add_argument("--skip-adversarial", action="store_true")
    ap.add_argument(
        "--safety-only",
        action="store_true",
        help="run only the adversarial set: no model calls, no cost",
    )
    ap.add_argument(
        "--adversarial-pipeline",
        action="store_true",
        help="also send the attack prompts through the model (costs tokens)",
    )
    ap.add_argument("--tag", default="default", help="label for the output artifact")
    args = ap.parse_args()

    settings = get_settings()
    pipeline = Pipeline(settings=settings)
    started = time.perf_counter()

    print(f"provider={pipeline.provider.name} model={settings.model} "
          f"strategy={settings.prompt_strategy} schema_format={settings.schema_format} "
          f"few_shot={settings.few_shot_count}\n")

    results: list[QuestionResult] = []
    if args.safety_only:
        print("safety only: skipping the golden set\n")
    else:
        print("golden set:")
        results = run_golden(pipeline, args.limit)
    summary = summarize(results)

    adversarial: dict[str, Any] = {}
    if not args.skip_adversarial:
        print("\nadversarial set (validator, model removed from the loop):")
        adversarial = run_adversarial(pipeline, with_pipeline=args.adversarial_pipeline)

    priced_model = settings.model if pipeline.provider.name == "anthropic" else "retrieval"
    total_cost = sum(
        estimate_cost(priced_model, r.input_tokens, r.output_tokens, r.cache_read_tokens)
        for r in results
    )

    payload = {
        "tag": args.tag,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.perf_counter() - started, 1),
        "config": {
            "provider": pipeline.provider.name,
            "model": settings.model,
            "prompt_strategy": settings.prompt_strategy,
            "schema_format": settings.schema_format,
            "few_shot_count": settings.few_shot_count,
            "max_retries": settings.max_retries,
        },
        "summary": summary,
        "estimated_total_cost_usd": round(total_cost, 4),
        "results": [asdict(r) for r in results],
        "adversarial": adversarial,
    }
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out = ARTIFACT_DIR / f"eval_{args.tag}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    if results:
        print("\n" + render_markdown(payload))
    lat = summary["latency_ms"]
    print(f"\nend-to-end latency  p50 {lat['end_to_end_p50']} ms   p95 {lat['end_to_end_p95']} ms")
    print(f"query execution     p50 {lat['query_execution_p50']} ms   "
          f"p95 {lat['query_execution_p95']} ms")
    if adversarial:
        print(
            f"safety block rate   {adversarial['validator_blocked']}/{adversarial['total']} "
            f"= {adversarial['block_rate'] * 100:.0f}% (validator, no model in the loop)"
        )
    print(f"estimated cost      ${total_cost:.4f} for {len(results)} questions")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
