# nl2sql

Natural-language questions answered as SQL over a realistic learning-platform database,
with safety and correctness enforced in code rather than requested in a prompt.

It is also a course in relational databases. The teaching artifacts are the point as much
as the system is: every performance claim in them was measured on this machine against 5
million rows, and the scripts that produced the numbers are in the repository.

```
question ──> prompt (schema from the live catalog + retrieved examples)
                │
                ▼
            Claude ──> JSON: { action: sql | clarify, sql, interpretation, assumptions }
                │
                ▼
         VALIDATOR      parse to an AST; read-only, real tables/columns, bounded LIMIT
                │        (never sees the question — that is what makes it a control)
                ▼
        PLAN GATE       EXPLAIN, without ANALYZE; reject above a cost threshold
                │        (nothing has executed yet)
                ▼
        EXECUTION       role with SELECT only · read-only txn · 5 s statement timeout
                │
                ▼
          ANSWER        rows + SQL + plan in plain English + what was actually asked
```

---

## Quick start

Needs PostgreSQL 16 and Python 3.11.

```bash
make install          # venv + dependencies
make db               # create, migrate, seed (100k learners / 5M submissions, ~2 min)
make index-exercise   # the teaching artifact: 10 queries before and after indexing
```

Then either use it or learn from it:

```bash
make run              # API + one-page frontend on http://127.0.0.1:8912
make safety           # 15 adversarial payloads through the validator — no model, no cost
make demos            # four short, runnable explanations
make drills           # 40 SQL exercises with a result-set checker
make check            # ruff + mypy --strict + 214 tests
```

To use the model you need a credential; without one the pipeline falls back to a
deterministic offline baseline and says so in every result.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make eval             # 60-question golden set + 15 adversarial cases
```

---

## Where to start reading

**If you want to learn SQL** — which is what this repo is for — go in this order:

1. **`make index-exercise`.** Ten queries, measured before and after indexing. This is the
   fastest way to see why any of the rest matters.
2. **[LEARN.md](LEARN.md)** — 16 concepts, each on five rungs from one sentence to
   annotated code. The worked-numbers rung is the one to do: a three-table join by hand on
   five rows per table, a B-tree lookup with the pages counted, and one real `EXPLAIN`
   annotated line by line.
3. **`make demos`** — four scripts under 60 lines: an index with and without, every join
   type with the rows printed, the N+1 problem with the query counts, and an attack being
   blocked with its parse tree shown.
4. **`drills/`** — 40 exercises, easy to hard, checked by comparing result sets rather than
   text. The last five make you add an index and prove it with `EXPLAIN`.
5. **[FAILURES.md](FAILURES.md)** — eight deliberate bugs, each run, with the damage
   measured.
6. **[INTERVIEW.md](INTERVIEW.md)** — 30 answers under 120 words, with the numbers to cite.

**If you want to understand the system**, read
[`src/nl2sql/validator.py`](src/nl2sql/validator.py) first. It is the component that makes
the difference between a demo and something you would point at a real database, and its
module docstring explains why it works on the AST instead of the string.

**If you want to build it yourself**, `exercises/` has the five load-bearing pieces
stripped to `NotImplementedError` with precise specs and the tests that verify your version.

---

## What is in here

| Path | What it is |
| --- | --- |
| `migrations/` | 9 sequenced migrations. 0009 (indexes) is held back on purpose |
| `db/seed/` | Generator: 100k learners, 5M submissions, lognormal skew, time-ordered |
| `db/indexing/` | The 10-query before/after exercise |
| `src/nl2sql/` | Schema introspection, prompting, validator, plan gate, pipeline, API |
| `eval/` | 60-question golden set, 15 adversarial cases, comparator, sweep |
| `bench/` | Latency distribution and the 10k→5M scaling curve |
| `failures/` | The 8 deliberate bugs, runnable |
| `demos/`, `drills/`, `exercises/` | Teaching artifacts |
| `tests/` | 214 tests, including every adversarial case individually |

---

## The database

Eight tables in 3NF, with one deliberate denormalisation.

```
learners ──< enrollments >── cohorts ──> courses ──< assignments
   │              │                                      │
   │              ├──< submissions >─────────────────────┘
   │              ├──< mentor_sessions >── learners (mentors are learners)
   └──────────────┴──< payments
```

`enrollments.course_id` duplicates `cohorts.course_id`. It is there because it enables a
covering index a join cannot have — 27x on the completion-rate query. It cannot drift,
because a composite foreign key to `cohorts (id, course_id)` with `ON UPDATE CASCADE` makes
a disagreeing row impossible. That mechanism is the difference between a denormalisation
and a bug, and `tests/test_schema.py` fails the build if it is removed.

The seed is deliberately skewed: submissions per enrollment are lognormal (median 11, p99
204, max 800), so the top 1% of enrollments hold ~13% of all submissions. Uniform random
data makes every planner estimate trivially correct, and then you never get to see one go
wrong — which is the thing worth learning. Rows are loaded in `submitted_at` order, giving
a physical correlation of 0.9999, which is what makes the 32 kB BRIN index viable.

---

## Headline measurements

Full detail and method in [BENCHMARKS.md](BENCHMARKS.md).

| | Result |
| --- | --- |
| Adversarial payloads blocked **at the validator, model not in the loop** | **15 / 15** |
| Same attacks against a prompt + keyword filter | **7 / 15** |
| Index exercise | median **28x**, best **2,987x**, one honest **1.0x** |
| Query latency at 5M rows | P50 **10.8 ms**, P95 **452 ms** |
| Point lookup, 10k → 5M rows | unindexed 0.29 → 69 ms; indexed 0.003 → 0.065 ms |
| Index storage overhead | 219 MB on 1,169 MB (**19%**) |
| Cost per question | **$0.0084** cached, $0.0221 uncached |
| Tests / lint / types | 214 passing · ruff clean · mypy `--strict` clean |

**Execution accuracy is not measured here.** There is no API credential in this
environment, so the pipeline resolves to the offline baseline and a real number cannot be
produced. Rather than print a plausible one, [BENCHMARKS.md §7](BENCHMARKS.md) leaves the
table empty and gives the command that fills it in. The offline baseline scores **0/60**,
which is the control that proves the evaluation measures generation rather than retrieval.

---

## Safety model

Four layers. Only one of them is the prompt, and it is the one that does not count.

| Layer | Mechanism | Fails when |
| --- | --- | --- |
| 1. Prompt | asks for read-only SQL | the user argues with it — i.e. immediately |
| 2. Validator | AST node-type checks, mandatory LIMIT | we have a bug |
| 3. Plan gate | `EXPLAIN` (never `ANALYZE`) against a cost budget | the estimate is wrong |
| 4. Role | `SELECT`-only grants + read-only txn + 5 s timeout | it never reads the user's input at all |

Layer 2 never receives the question — `validate(sql, catalog, settings)` has no parameter
through which a prompt could reach it, and a test asserts the validator does not import the
prompt module. Layer 4 was verified by turning off the read-only flag and watching the
grants refuse the write anyway.

---

## Honest limitations

* **Accuracy is unmeasured** in this environment. See above.
* **The sweep is one-factor-at-a-time**, not a full grid, so it cannot see interactions
  between prompt strategy and schema format. The reasoning and the cost are in
  `eval/sweep.py`.
* **Column-permutation tolerance** in the comparator accepts a query that swapped two
  columns holding identical values. That is the price of not failing correct queries over
  cosmetics.
* **Mentors are modelled as `learners` with a flag** because the brief fixes the schema at
  eight tables. A real platform would give them their own table.
* **`ix_submissions_enrollment_time` is 69% of all index storage** and buys 20x on one
  query shape, because the `UNIQUE` index already covers the filtering. It is arguably not
  worth it. The measurement is in BENCHMARKS §3 so you can disagree with me.
