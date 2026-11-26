"""Sweep prompting strategy, few-shot count and schema serialisation format.

WHY ONE FACTOR AT A TIME AND NOT THE FULL GRID
----------------------------------------------
The full cross product is 3 strategies x 4 few-shot counts x 4 schema formats = 48
configurations. At 60 questions each that is 2,880 model calls per sweep, which is both
slow and expensive enough that you would run it once and then never again -- and a
measurement you only take once is a measurement you cannot use to decide anything.

So the sweep varies one axis at a time from a baseline: 3 + 4 + 4 - 2 shared baselines =
9 configurations, 540 calls. The honest cost of that choice is that it cannot see
INTERACTIONS. If markdown serialisation only helps when you also have eight examples, this
design will miss it. That is a real limitation and the right response is to note it, then
run the full grid on the two axes that mattered once the cheap sweep has narrowed them.

WHAT IS MEASURED WITHOUT A MODEL
--------------------------------
Prompt size is a property of the prompt, not of the model, so token counts per
configuration are measured on every run regardless of whether an API key is present. That
is what makes the cost table in BENCHMARKS.md real rather than estimated: the JSON schema
format costs 4.9x the tokens of the compact format on every single request forever, and
you can know that before you spend anything finding out whether it is worth it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from eval.golden_set import GOLDEN_SET  # noqa: E402
from eval.run_eval_learners import estimate_cost, run_golden, summarize  # noqa: E402
from nl2sql.config import get_settings  # noqa: E402
from nl2sql.llm import get_provider  # noqa: E402
from nl2sql.pipeline import Pipeline  # noqa: E402
from nl2sql.prompt import PROMPT_STRATEGIES, build_prompt  # noqa: E402
from nl2sql.schema import SCHEMA_FORMATS, get_catalog  # noqa: E402

ARTIFACT_DIR = ROOT / "eval" / "artifacts"
FEW_SHOT_COUNTS = (0, 2, 4, 8)

BASELINE = {"strategy": "structured", "schema_format": "ddl", "few_shot_count": 4}


@dataclass
class SweepConfig:
    axis: str
    strategy: str
    schema_format: str
    few_shot_count: int

    @property
    def label(self) -> str:
        return f"{self.strategy}/{self.schema_format}/k={self.few_shot_count}"


@dataclass
class SweepRow:
    axis: str
    label: str
    strategy: str
    schema_format: str
    few_shot_count: int
    median_prompt_tokens: int
    max_prompt_tokens: int
    accuracy: float | None = None
    by_category: dict[str, float] = field(default_factory=dict)
    cost_usd: float | None = None
    latency_p50_ms: float | None = None


def configurations() -> list[SweepConfig]:
    """Baseline plus one variation per axis, de-duplicated."""
    seen: set[tuple[str, str, int]] = set()
    out: list[SweepConfig] = []

    def add(axis: str, strategy: str, fmt: str, k: int) -> None:
        key = (strategy, fmt, k)
        if key in seen:
            return
        seen.add(key)
        out.append(SweepConfig(axis, strategy, fmt, k))

    add("baseline", **{"strategy": BASELINE["strategy"], "fmt": BASELINE["schema_format"],  # type: ignore[arg-type]
                       "k": BASELINE["few_shot_count"]})  # type: ignore[arg-type]
    for strategy in PROMPT_STRATEGIES:
        add("prompt_strategy", strategy, str(BASELINE["schema_format"]),
            int(BASELINE["few_shot_count"]))
    for fmt in SCHEMA_FORMATS:
        add("schema_format", str(BASELINE["strategy"]), fmt, int(BASELINE["few_shot_count"]))
    for k in FEW_SHOT_COUNTS:
        add("few_shot_count", str(BASELINE["strategy"]), str(BASELINE["schema_format"]), k)
    return out


def measure_prompt_sizes(cfg: SweepConfig) -> tuple[int, int]:
    """Median and max prompt size across the golden questions, in approximate tokens.

    Approximate because the exact count needs the API. AnthropicProvider.count_tokens()
    gives the exact figure when a key is present; the ratio between configurations, which
    is what the sweep is comparing, is stable either way.
    """
    catalog = get_catalog()
    sizes = []
    for q in GOLDEN_SET:
        prompt = build_prompt(
            q.question,
            catalog,
            strategy=cfg.strategy,  # type: ignore[arg-type]
            schema_format=cfg.schema_format,  # type: ignore[arg-type]
            few_shot_count=cfg.few_shot_count,
        )
        sizes.append(prompt.approx_tokens())
    sizes.sort()
    return sizes[len(sizes) // 2], sizes[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--prompt-size-only",
        action="store_true",
        help="measure token cost per configuration without calling the model",
    )
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    settings = get_settings()
    provider = get_provider(settings)
    have_model = provider.name == "anthropic"
    run_accuracy = have_model and not args.prompt_size_only

    if not run_accuracy:
        reason = (
            "--prompt-size-only was passed"
            if args.prompt_size_only
            else f"no model credential found (provider resolved to {provider.name!r})"
        )
        print(f"note: accuracy columns will be blank because {reason}.\n")

    configs = configurations()
    print(f"sweeping {len(configs)} configurations\n")
    rows: list[SweepRow] = []
    started = time.perf_counter()

    for cfg in configs:
        median_tokens, max_tokens = measure_prompt_sizes(cfg)
        row = SweepRow(
            axis=cfg.axis,
            label=cfg.label,
            strategy=cfg.strategy,
            schema_format=cfg.schema_format,
            few_shot_count=cfg.few_shot_count,
            median_prompt_tokens=median_tokens,
            max_prompt_tokens=max_tokens,
        )
        if run_accuracy:
            pipeline = Pipeline(provider=provider, settings=settings)
            results = run_golden_quiet(pipeline, cfg, args.limit)
            summary = summarize(results)
            row.accuracy = summary["overall"]["accuracy"]
            row.by_category = {
                cat: data["accuracy"] for cat, data in summary["by_category"].items()
            }
            row.latency_p50_ms = summary["latency_ms"]["end_to_end_p50"]
            row.cost_usd = round(
                sum(
                    estimate_cost(
                        settings.model, r.input_tokens, r.output_tokens, r.cache_read_tokens
                    )
                    for r in results
                ),
                4,
            )
        rows.append(row)
        acc = f"{row.accuracy * 100:5.1f}%" if row.accuracy is not None else "    --"
        print(f"  {cfg.axis:16} {cfg.label:26} {median_tokens:6,} tok  acc={acc}")

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.perf_counter() - started, 1),
        "accuracy_measured": run_accuracy,
        "provider": provider.name,
        "baseline": BASELINE,
        "rows": [vars(r) for r in rows],
    }
    out = ARTIFACT_DIR / "sweep.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n" + render_table(rows, run_accuracy))
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


def run_golden_quiet(pipeline: Pipeline, cfg: SweepConfig, limit: int | None) -> list[Any]:
    """Run the golden set with a configuration override."""
    original = (
        pipeline.settings.prompt_strategy,
        pipeline.settings.schema_format,
        pipeline.settings.few_shot_count,
    )
    pipeline.settings.prompt_strategy = cfg.strategy
    pipeline.settings.schema_format = cfg.schema_format
    pipeline.settings.few_shot_count = cfg.few_shot_count
    try:
        return run_golden(pipeline, limit)
    finally:
        (
            pipeline.settings.prompt_strategy,
            pipeline.settings.schema_format,
            pipeline.settings.few_shot_count,
        ) = original


def render_table(rows: list[SweepRow], with_accuracy: bool) -> str:
    header = "| axis | configuration | median prompt tokens | relative size |"
    sep = "| --- | --- | --- | --- |"
    if with_accuracy:
        header += " accuracy | cost / 60 q |"
        sep += " --- | --- |"
    baseline_tokens = next(
        (r.median_prompt_tokens for r in rows if r.axis == "baseline"), rows[0].median_prompt_tokens
    )
    lines = [header, sep]
    for r in rows:
        rel = r.median_prompt_tokens / baseline_tokens if baseline_tokens else 1.0
        line = f"| {r.axis} | {r.label} | {r.median_prompt_tokens:,} | {rel:.2f}x |"
        if with_accuracy:
            acc = f"{r.accuracy * 100:.1f}%" if r.accuracy is not None else "--"
            cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "--"
            line += f" {acc} | {cost} |"
        lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
