# OWNERSHIP_DRILL

Goal: be able to defend every claim in this repo without opening the code. File and
function references are exact as of this branch (`hardening-v2`); line numbers drift, names don't.

## 1. The pipeline (one request)

```
  POST /api/ask  {question}                                   api.py::ask
        |
        v
  [1] SCHEMA LINK      linker.py::link_schema                 BM25 over table docs, top-k seeds,
        |              (prompt only; never widens security)   + 1-hop FK neighbours, cap 5 tables
        v                                                       nothing matches -> full schema
  [2] LLM GENERATE     llm.py::AnthropicProvider.generate     returns JSON {action, sql, ...}
        |                                                       (clarify is a first-class outcome)
        v
  [3] AST GATE         validator.py::validate                 sqlglot.parse(read=postgres)
        |                                                       one statement | read root | no write node
        |                                                       no pg_*/dblink/lo_* funcs | no catalogs
        |                                                       no CROSS JOIN | columns resolve | LIMIT
        |--- HARD_BLOCK_CODES (pipeline.py) ---> 403, 0 retries, never fed back to the model
        |--- RETRYABLE_CODES ------------------> error text -> back to [2]   (max 2 retries)
        v
  [4] EXPLAIN GATE     plan_gate.py::check_plan               EXPLAIN (FORMAT JSON), no ANALYZE
        |                db.py::_flatten (recursive)            cost > budget -> block
        |                                                       selective Seq Scan on big table -> block
        |--- block -> error text -> back to [2] (same retry budget)
        v
  [5] EXECUTE          db.py::fetch_all / readonly_cursor     role nl2sql_ro: SELECT only,
        |                                                       read-only txn, 3 s statement_timeout,
        |                                                       work_mem 16MB   (db/init_roles.sql)
        v
  answer + telemetry   telemetry.py::record   one JSON line: latency_ms, retries_used,
                                              tables_selected, estimated_cost (no question/SQL/rows)
```

Defense in depth, weakest-link view: [3] and [4] are *our* code and can have bugs; [5] is the
database refusing on its own. `tests/test_security_integration.py` attacks each layer separately.

## 2. The 10 hardest questions

**Q1. "Why isn't 'only generate SELECTs' in the prompt a security control?"**
The prompt is a request in the same channel the attacker types into. `validator.py::validate(sql, catalog, settings)`
takes no question parameter, so no wording of a question can influence the verdict. Backstop:
`pipeline.py::HARD_BLOCK_CODES` means a write is never even sent back to the model to "fix".
Proof: `test_security_violation_is_never_retried` (provider asked exactly once).

**Q2. "A root-node `startswith SELECT` check passes `WITH d AS (DELETE ... RETURNING *) SELECT * FROM d`. How do you?"**
`validator.py::_check_read_only` walks the *entire* tree and rejects any node in `WRITE_NODES`
(`WRITE_IN_SUBTREE`), not just the root. Test: parametrized case "data-modifying CTE" in
`test_ast_gate_rejects`. Even if that failed, `nl2sql_ro` has no DELETE privilege (Q4).

**Q3. "Why sqlglot AST and not regex?"**
Statement count comes from `sqlglot.parse(...)` in `_check_single_statement` (a `;` inside a string
literal is harmless, a second statement hidden after a comment is not); function names come from
`exp.Func` nodes (`_check_functions`), so `/**/pg_sleep/**/` and `PG_SLEEP` normalise to one name;
`dropped_at` the column is not `DROP`. Known limit: pinned `sqlglot<30` because v30 removed
`qualify(infer_schema=)` and the validator failed closed on every query (AUDIT finding 1).

**Q4. "If your validator has a bug, what stops `DELETE FROM payments`?"**
The database. `db/init_roles.sql` grants `nl2sql_ro` only SELECT. `test_database_refuses_writes_and_secrets_even_if_every_gate_is_bypassed`
connects as that role with no validator at all. `test_role_cannot_switch_off_read_only_and_write` is the
honest nuance: `default_transaction_read_only` is user-overridable (`SET ... = off` succeeds), so the
*privilege* is the wall, and the read-only flag is the second layer (`db.py::readonly_cursor`).

**Q5. "Where is the 3-second timeout enforced and what does it cost you?"**
Three places: `ALTER ROLE nl2sql_ro SET statement_timeout` (`db/init_roles.sql`), the pool's per-connection `SET`
(`db.py::_make_pool.configure`), and `config.py::statement_timeout_ms`. `test_statement_timeout_applies_to_a_raw_role_connection`
proves the role alone enforces it (no client SET). Cost: the legacy 5M-row query `jn09` takes ~2.9 s cold, so
`tests/conftest.py` raises the limit for the legacy DB only. 3 s is sized for 50k rows.

**Q6. "Why EXPLAIN without ANALYZE, and what exactly do you reject?"**
`db.py::explain(analyze=False)` asks what the planner *intends*; ANALYZE would run the query. `db.py::_flatten`
recurses through `Plans`. `plan_gate.py::check_plan` blocks (a) `Total Cost > max_plan_cost`, and (b)
`SELECTIVE_SEQ_SCAN_LARGE_TABLE`: a filtered Seq Scan on a table over `large_table_rows` that keeps <= 5%
(`SELECTIVE_FRACTION`) of rows, i.e. a missing index. Unfiltered scans only WARN (an aggregate needs them).
Weakness: it trusts planner statistics (`pg_class.reltuples`); stale stats mean wrong gates.

**Q7. "How do you detect a CROSS JOIN, and how would I beat it?"**
`validator.py::_is_cross_join` / `_check_cross_joins`: explicit CROSS, `JOIN ... ON true`/`1=1`, or a bare comma
join of two real tables. Subquery/CTE/function right sides are allowed (bounded by construction). Beat it: a comma
join where *some* `WHERE a.x = b.y` exists but a third table is unjoined, since `_has_column_equality` is a heuristic.
The backstop is the cost gate: `test_plan_gate_blocks_high_cost_plan`. A CROSS_JOIN is retryable (honest mistake), not a 403.

**Q8. "What does schema linking buy you, and can it weaken security?"**
`linker.py::link_schema`: `bm25_scores` over per-table documents (name x3, comments, columns, enum + sample values),
top-k seeds, then 1-hop FK neighbours up to `max_tables`; no match returns the full schema. It narrows only the
*prompt*; `validate()` always uses the full catalog, so a miss makes an answer wrong, never wider. A hidden-table miss is
recovered: `pipeline.py::_run` widens to the full schema on `UNKNOWN_TABLE/COLUMN` retries. Measured (EVAL_RESULTS §4):
recall 47/47 at 4.51 of 7 tables, **in-sample** (default chosen after seeing the sweep). On 7 tables the saving is small.
Sample values skip PII columns (`schema.py::_PII_COLUMN`) because they are sent to a third-party LLM.

**Q9. "Which errors are retried, which return 403, and why cap at 2?"**
`pipeline.py::RETRYABLE_CODES` (parse error, unknown table/column, missing/too-large LIMIT, CROSS_JOIN, ...) vs
`HARD_BLOCK_CODES` (write, system catalog, forbidden function, stacked statements) which set
`Answer.security_violation` and `api.py::ask` returns HTTP 403. Cap = `config.py::max_retries` = 2, so at most 3
provider calls per question (`test_retry_budget_is_hard_capped_at_two`); unbounded retries is how you spend money without
converging. Telemetry (`telemetry.py::record`) logs latency/retries/tables/cost but never the question, SQL or rows.

**Q10. "What do your numbers actually prove?"**
Read `EVAL_RESULTS.md` literally. 47/47 security tests and 265/265 suite are real runs. The oracle/faulty rows verify
the *harness and gates* (reference SQL scores 100% by construction; faulty isolates the retry mechanism); they are not
model accuracy. "Unsafe rejection 10/10" injects the attack SQL, model removed: it shows the gates stop emitted
attacks, not that an LLM can't be jailbroken into emitting them. No live-LLM figure exists until
`run_eval.py --provider anthropic` is run. Latency is `run_eval.py::percentile` (nearest-rank) over `Pipeline.answer()`.

## 3. Two files to rewrite from scratch (do this blind, then diff against the original)

1. **`src/nl2sql/validator.py`** (the AST gate). Spec: `validate(sql, catalog, settings) -> ValidationResult`; one
   statement; read-only root and no write node anywhere in the tree; deny function names + prefixes; deny system
   schemas / `pg_*` tables; cross-join rule; column resolution with `sqlglot.optimizer.qualify`; LIMIT required unless
   the query is a scalar aggregate (`is_scalar_query`). Verify with `tests/test_validator.py`,
   `tests/test_linker_offline.py::test_scalar_detection` and the 23 parametrized cases in `tests/test_security_integration.py`.
2. **`src/nl2sql/pipeline.py::Pipeline._run`** (the control loop). Spec: link -> generate -> validate -> EXPLAIN gate ->
   execute; max 2 retries on fixable errors with the error text fed back; hard-block codes return immediately with
   `security_violation=True`; widen the schema on unknown table/column; one telemetry record per call. Verify with
   `tests/test_pipeline.py` and the three pipeline tests in `tests/test_security_integration.py`.
