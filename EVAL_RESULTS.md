# EVAL_RESULTS

Every figure below is read from an artifact written by a real run on this machine (`eval/render_results.py` regenerates this file; nothing is typed by hand).

## 1. Test suites (real PostgreSQL 16 in Docker, port 5433)

| Suite | Tests | Passed | Failed | Skipped | Wall time |
|---|---|---|---|---|---|
| `tests/test_security_integration.py` (live-DB adversarial) | 47 | 47 | 0 | 0 | 6.7s |
| Entire `tests/` directory | 265 | 265 | 0 | 0 | 34.2s |

One earlier full-suite run in this session failed 1 test (legacy `jn09`, a 5M-row aggregate that timed out on a cold cache while Docker was starting); the rerun recorded above is 0 failures. That legacy test is timing-sensitive.

The entire-suite row includes the pre-existing learners-database tests, which run against a separate local 5M-row PostgreSQL on :5432 (not part of this repo's Docker setup).

## 2. Harness verification: oracle provider (NO LLM)

The oracle returns the golden reference SQL / the adversarial attack SQL. It proves the harness, the dataset and the safety gates are wired correctly (reference = 100% by construction; every attack must be stopped by the gates). **These are not model-accuracy numbers.** Latency here contains no LLM time.

Dataset: 60 questions; finished 2026-11-26T12:23:19Z.

| Ablation | Execution Accuracy | Unsafe Rejection | Unanswerable handled | p50 ms | p95 ms | Retries used |
|---|---|---|---|---|---|---|
| baseline | 47/47 (100.0%) | 10/10 (100.0%) | 3/3 (100.0%) | 4.18 | 12.91 | 0 |
| +schema_linking | 47/47 (100.0%) | 10/10 (100.0%) | 3/3 (100.0%) | 4.21 | 11.45 | 0 |
| +error_retries | 47/47 (100.0%) | 10/10 (100.0%) | 3/3 (100.0%) | 4.35 | 10.85 | 4 |

Execution accuracy by difficulty (last ablation): easy 15/15 (100.0%), hard 14/14 (100.0%), medium 18/18 (100.0%)

Of the 10 unsafe items, 8 were classed as security violations (HTTP 403, zero retries); the rest are resource-abuse shapes (CROSS JOIN) that are rejected after the bounded retry budget.

## 3. Harness verification: retry mechanism (fault injection, NO LLM)

Same as the oracle, except the first attempt of every 3rd answerable question references a nonexistent column. It can only recover if the validator error is fed back and the retry loop runs, so the jump between `+schema_linking` and `+error_retries` isolates the retry mechanism. **Not a model result.**

Dataset: 60 questions; finished 2026-11-26T12:23:20Z.

| Ablation | Execution Accuracy | Unsafe Rejection | Unanswerable handled | p50 ms | p95 ms | Retries used |
|---|---|---|---|---|---|---|
| baseline | 31/47 (66.0%) | 10/10 (100.0%) | 3/3 (100.0%) | 3.47 | 8.0 | 0 |
| +schema_linking | 31/47 (66.0%) | 10/10 (100.0%) | 3/3 (100.0%) | 3.79 | 9.55 | 0 |
| +error_retries | 47/47 (100.0%) | 10/10 (100.0%) | 3/3 (100.0%) | 6.23 | 22.21 | 20 |

Execution accuracy by difficulty (last ablation): easy 15/15 (100.0%), hard 14/14 (100.0%), medium 18/18 (100.0%)

Of the 10 unsafe items, 8 were classed as security violations (HTTP 403, zero retries); the rest are resource-abuse shapes (CROSS JOIN) that are rejected after the bounded retry budget.

## 4. Schema linking (deterministic, no LLM)

Recall = share of the 47 answerable questions whose linked table set contains every table the reference SQL touches. Computed against the live 7-table catalog.

| top_k seeds | max tables | Recall | Mean tables in prompt (of 7) |
|---|---|---|---|
| 1 | 3 | 43/47 (91.5%) | 2.85 |
| 2 | 4 | 45/47 (95.7%) | 3.81 |
| 2 | 5 **(default)** | 47/47 (100.0%) | 4.51 |
| 3 | 5 | 47/47 (100.0%) | 4.62 |
| 3 | 7 | 47/47 (100.0%) | 5.13 |

Misses at the default setting: none.

Caveat: the default (2, 5) was chosen after seeing this sweep, on the same 47 questions it is scored on, so its recall is in-sample and optimistic. It also shows how little a 7-table schema can be narrowed: linking pays off as the table count grows.

## 5. Live LLM ablation (Baseline vs +Schema Linking vs +Error Retries)

**NOT RUN: no `ANTHROPIC_API_KEY` was exported in the environment where this was produced.** No LLM-accuracy figure exists in this repository yet. Run exactly:

```bash
ANTHROPIC_API_KEY=sk-ant-... .venv312/bin/python eval/run_eval.py --provider anthropic && .venv312/bin/python eval/render_results.py
```

