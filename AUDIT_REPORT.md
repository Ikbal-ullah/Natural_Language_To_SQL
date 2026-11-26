# Audit report (pre-hardening, commit 750d930)

Scope: every file under `src/`, `db/`, `eval/`, `tests/`, `migrations/`, `Makefile`, `README.md`.
Every finding below was reproduced on this machine; none is inferred.

## What was already solid (kept, not rewritten)
| Area | Where | Verdict |
|---|---|---|
| AST validator: single statement, read-only root, write-in-subtree (data-modifying CTE), `SELECT INTO`, locking clauses, schema/column resolution | `src/nl2sql/validator.py` | Good design: it never sees the question. |
| `EXPLAIN (FORMAT JSON)` plan gate, recursive plan flatten | `src/nl2sql/plan_gate.py`, `db.py::_flatten` | Good; gaps below. |
| Read-only role + read-only txn + `psycopg_pool` | `migrations/0008_readonly_role.sql`, `db.py` | Good; gaps below. |
| Bounded retry loop, hard-block codes not retried | `pipeline.py` | Good. |
| Baseline test suite | `tests/` | 214 pass (after finding 1). |

## Findings
| # | Sev | Finding | Evidence |
|---|---|---|---|
| 1 | High | `sqlglot>=25.0` is unpinned. On sqlglot 30.35 `qualify(infer_schema=...)` no longer exists, so **every** query fails column verification (fail-closed outage; 87 of 214 tests fail). | `TypeError: qualify() got an unexpected keyword argument 'infer_schema'` |
| 2 | High | Forbidden functions are an exact-name list. `current_setting('data_directory')` and `pg_ls_waldir()` pass the validator. | probe run, both returned `ok=True` |
| 3 | High | No guard against unbounded `CROSS JOIN` / comma joins between two base tables (`learners CROSS JOIN submissions` -> `ok=True`). Only the plan cost gate would catch it, and only after the planner is asked. | probe run |
| 4 | Med | A large-table `Seq Scan` is only ever a WARN, so a selective filter on an unindexed column of a big table is never refused. | `plan_gate.py::check_plan` |
| 5 | Med | Scalar aggregates (`SELECT count(*) ...`) are reported as `MISSING_LIMIT`; LIMIT is injected indiscriminately rather than on non-scalar queries only. | probe run |
| 6 | Med | Connection pools are cached by privilege (`"ro"`/`"admin"`), not by DSN: a second database in the same process silently reuses the first pool. | `db.py::get_pool` |
| 7 | Med | Statement timeout is 5 s (role + pool), `work_mem` is not bounded on the role. | `0008_readonly_role.sql`, `config.py` |
| 8 | Med | HTTP layer returns `200` with `status: "rejected"` for a security violation; no structured per-request telemetry. | `api.py::ask` |
| 9 | Med | No schema linking: the entire schema goes in every prompt; no sample values. Does not scale past a handful of tables. | `prompt.py::build_prompt` |
| 10 | Med | Prompt role, rules and schema notes are hard-wired to the learning-platform domain (`submissions`, integer cents...). A second schema would get wrong instructions. | `prompt.py`, `serialize.py::SCHEMA_NOTES` |
| 11 | Low | No `docker-compose.yml`, no CI, no self-contained dataset: the existing DB needs a local PostgreSQL, 5M-row seed (~2 min) and a manual role migration. | repo root |
| 12 | Low | `.venv` is Python 3.15 and has **no dependencies installed**; tests cannot run in it. A Python 3.12 venv is used instead. | `ModuleNotFoundError: pydantic` |
| 13 | Info | Existing eval (`eval/run_eval.py`) targets the learners DB; it has no schema-linking / retry ablation and no p50/p95 latency by ablation. It is preserved as `eval/run_eval_learners.py`. | |

## Plan
Fix 1-12 in separate commits; add an e-commerce dataset + eval that exercise the ablations; record
only measured numbers in `EVAL_RESULTS.md`.
