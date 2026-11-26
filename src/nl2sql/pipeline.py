"""The end-to-end pipeline: question in, checked answer out.

    build prompt -> generate -> validate -> plan gate -> execute -> explain
                        ^                                   |
                        +--------- bounded retry <----------+

WHAT THE RETRY LOOP IS AND IS NOT
---------------------------------
It is a repair mechanism for mistakes a model can actually fix from a precise error
message: a hallucinated column, a missing LIMIT, a join that produces a plan too expensive
to run. The feedback handed back is the validator's structured output, not a paraphrase --
"[UNKNOWN_COLUMN] Column 'submitted_date' does not exist" is repairable, "that was wrong"
is not.

It is NOT a way to eventually get any question answered. The budget is small and fixed
(default 2 retries, so 3 model calls at most) for reasons that are easy to underestimate:

  * Cost is linear in attempts and there is no natural stopping point. An unbounded loop
    on a question the model cannot answer spends money until something else breaks.
  * Latency is what the user experiences. Three sequential model calls is already several
    seconds.
  * Retries do not converge on hard questions. They oscillate -- the model alternates
    between two wrong queries, each fixing the objection to the other. Attempt 8 is not
    closer than attempt 3, it is just more expensive.

FAILURES.md #5 removes the bound and measures what actually happens.

Note also which failures are NOT retried: a database connection error, a statement
timeout, a model refusal. None of those are caused by the SQL being wrong, so re-asking
the model is guaranteed waste.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg

from nl2sql import telemetry
from nl2sql.config import Settings, get_settings
from nl2sql.db import fetch_all
from nl2sql.linker import link_schema
from nl2sql.llm import LLMError, LLMResponse, Provider, get_provider
from nl2sql.plan_gate import GateResult, Verdict, check_plan
from nl2sql.prompt import PromptStrategy, build_prompt
from nl2sql.schema import Catalog, SchemaFormat, get_catalog
from nl2sql.validator import Code, ValidationResult, add_limit, validate

Status = Literal["ok", "clarify", "rejected", "error"]


@dataclass
class Attempt:
    """One trip round the loop, recorded so the UI and the evaluation can show the path."""

    index: int
    sql: str | None
    stage: str
    ok: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class Answer:
    """Everything the caller needs to judge whether to trust the number."""

    status: Status
    question: str
    sql: str | None = None
    interpretation: str = ""
    assumptions: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    plan_summary: str = ""
    plan_findings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    elapsed_ms: float = 0.0
    generation_ms: float = 0.0
    execution_ms: float = 0.0
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost: float = 0.0
    # --- hardening telemetry ---
    tables_selected: list[str] = field(default_factory=list)  # tables shown to the model
    schema_linking: bool = False
    plan_cost: float = 0.0              # EXPLAIN total cost of the executed/last plan
    security_violation: bool = False    # True -> HTTP 403, zero retries

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def retries(self) -> int:
        """Generation rounds after the first.

        Not len(attempts) - 1: a single successful round records one attempt per STAGE
        (plan, then execute), so counting entries reports a retry that never happened.
        Attempt.index is the loop counter, so its maximum is the number of retries.
        """
        return max((a.index for a in self.attempts), default=0)


# Errors the model can plausibly repair. Anything else is an infrastructure problem and
# retrying it just burns the budget.
RETRYABLE_CODES = frozenset(
    {
        Code.PARSE_ERROR, Code.EMPTY, Code.UNKNOWN_TABLE,
        Code.UNKNOWN_COLUMN, Code.AMBIGUOUS_COLUMN, Code.MISSING_LIMIT,
        Code.LIMIT_TOO_LARGE, Code.SELECT_INTO, Code.LOCKING_CLAUSE, Code.CROSS_JOIN,
    }
)
# Deliberately NOT retryable: a write, a system-catalog read, a forbidden function, or a
# stacked statement.
# Those are not honest mistakes to be corrected, they are the thing the validator exists
# to stop, and feeding them back to the model to "fix" is how you end up with a system
# that negotiates with an attacker. Blocked once, blocked.
HARD_BLOCK_CODES = frozenset(
    {
        Code.NOT_A_READ, Code.WRITE_IN_SUBTREE, Code.SYSTEM_CATALOG, Code.FORBIDDEN_FUNCTION,
        Code.MULTIPLE_STATEMENTS,  # stacked queries are the classic injection shape
    }
)


def _repairable_limit_only(result: ValidationResult) -> bool:
    return bool(result.errors) and all(
        i.code is Code.MISSING_LIMIT for i in result.errors
    )


class Pipeline:
    """Owns the loop. One instance per process; it is stateless per call."""

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        catalog: Catalog | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider or get_provider(self.settings)
        self.catalog = catalog or get_catalog()

    # -- helpers ---------------------------------------------------------
    def _generate(
        self,
        question: str,
        *,
        strategy: PromptStrategy,
        schema_format: SchemaFormat,
        few_shot_count: int,
        feedback: str | None,
        previous_sql: str | None,
        prompt_catalog: Catalog | None = None,
    ) -> LLMResponse:
        prompt = build_prompt(
            question,
            prompt_catalog or self.catalog,
            domain=self.settings.domain,
            strategy=strategy,
            schema_format=schema_format,
            few_shot_count=few_shot_count,
            error_feedback=feedback,
            previous_sql=previous_sql,
        )
        return self.provider.generate(prompt)

    # -- entry point -----------------------------------------------------
    def answer(
        self,
        question: str,
        *,
        strategy: PromptStrategy | None = None,
        schema_format: SchemaFormat | None = None,
        few_shot_count: int | None = None,
        max_retries: int | None = None,
        execute: bool = True,
        schema_linking: bool | None = None,
    ) -> Answer:
        """Schema link -> LLM generate -> AST gate -> EXPLAIN gate -> execute.

        Emits one structured telemetry record per call. See `_run` for the loop.
        """
        result = self._run(
            question,
            strategy=strategy,
            schema_format=schema_format,
            few_shot_count=few_shot_count,
            max_retries=max_retries,
            execute=execute,
            schema_linking=schema_linking,
        )
        telemetry.record(result)
        return result

    def _run(
        self,
        question: str,
        *,
        strategy: PromptStrategy | None,
        schema_format: SchemaFormat | None,
        few_shot_count: int | None,
        max_retries: int | None,
        execute: bool,
        schema_linking: bool | None,
    ) -> Answer:
        started = time.perf_counter()
        strategy = strategy or self.settings.prompt_strategy  # type: ignore[assignment]
        schema_format = schema_format or self.settings.schema_format  # type: ignore[assignment]
        few_shot_count = (
            few_shot_count if few_shot_count is not None else self.settings.few_shot_count
        )
        budget = max_retries if max_retries is not None else self.settings.max_retries

        answer = Answer(status="error", question=question, provider=self.provider.name)
        use_linking = self.settings.schema_linking if schema_linking is None else schema_linking
        prompt_catalog = self.catalog
        answer.schema_linking = use_linking
        if use_linking:
            link = link_schema(
                question, self.catalog,
                top_k=self.settings.link_top_k, max_tables=self.settings.link_max_tables,
            )
            prompt_catalog = link.catalog
        answer.tables_selected = sorted(prompt_catalog.tables)
        feedback: str | None = None
        previous_sql: str | None = None
        generation_ms = 0.0

        for index in range(budget + 1):
            # ---------------- generate ----------------
            gen_start = time.perf_counter()
            try:
                llm = self._generate(
                    question,
                    strategy=strategy,  # type: ignore[arg-type]
                    schema_format=schema_format,  # type: ignore[arg-type]
                    few_shot_count=few_shot_count,
                    feedback=feedback,
                    previous_sql=previous_sql,
                    prompt_catalog=prompt_catalog,
                )
            except LLMError as err:
                answer.attempts.append(Attempt(index, None, "generate", False, [str(err)]))
                answer.status = "error"
                answer.error = str(err)
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                return answer
            generation_ms += (time.perf_counter() - gen_start) * 1000

            answer.model = llm.model
            answer.interpretation = llm.interpretation
            answer.assumptions = llm.assumptions
            answer.input_tokens += llm.input_tokens
            answer.output_tokens += llm.output_tokens
            answer.cache_read_tokens += llm.cache_read_tokens
            answer.cache_write_tokens += llm.cache_write_tokens

            if llm.wants_clarification:
                answer.attempts.append(Attempt(index, None, "clarify", True))
                answer.status = "clarify"
                answer.clarification_question = llm.clarification_question
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer

            sql = llm.sql or ""

            # ---------------- validate ----------------
            result = validate(sql, self.catalog, self.settings)
            if not result.ok and _repairable_limit_only(result):
                # A missing LIMIT is repaired here rather than by asking the model again.
                # It is a deterministic AST edit with exactly one correct answer, so
                # spending a model call and two seconds of latency on it would be silly.
                sql = add_limit(sql, self.settings.default_limit)
                result = validate(sql, self.catalog, self.settings)
                answer.warnings.append(
                    f"No LIMIT was generated; LIMIT {self.settings.default_limit} was added."
                )

            if not result.ok:
                codes = {i.code for i in result.errors}
                answer.attempts.append(
                    Attempt(index, sql, "validate", False, [i.render() for i in result.errors])
                )
                if codes & HARD_BLOCK_CODES:
                    answer.security_violation = True  # 403, and never retried
                    answer.status = "rejected"
                    answer.sql = sql
                    answer.error = result.feedback()
                    answer.elapsed_ms = (time.perf_counter() - started) * 1000
                    answer.generation_ms = generation_ms
                    return answer
                if index < budget and codes & RETRYABLE_CODES:
                    if codes & {Code.UNKNOWN_TABLE, Code.UNKNOWN_COLUMN, Code.AMBIGUOUS_COLUMN}:
                        # The linker may have hidden the table the model needed: widen to the
                        # full schema for the remaining attempts (recoverable recall miss).
                        prompt_catalog = self.catalog
                        answer.tables_selected = sorted(prompt_catalog.tables)
                    feedback = result.feedback()
                    previous_sql = sql
                    continue
                answer.status = "rejected"
                answer.sql = sql
                answer.error = result.feedback()
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer

            # ---------------- plan gate ----------------
            try:
                gate: GateResult = check_plan(sql, settings=self.settings)
            except psycopg.Error as err:
                # EXPLAIN failed, which means the query is not runnable at all -- a type
                # mismatch, say. That is a model-fixable error, so it goes round the loop.
                message = str(err).strip().splitlines()[0]
                answer.attempts.append(Attempt(index, sql, "plan", False, [message]))
                if index < budget:
                    feedback = f"[SQL_ERROR] PostgreSQL rejected the query: {message}"
                    previous_sql = sql
                    continue
                answer.status = "error"
                answer.sql = sql
                answer.error = message
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer

            answer.plan_summary = gate.summary
            answer.plan_cost = gate.plan.total_cost
            answer.plan_findings = [f.message for f in gate.findings]

            if gate.verdict is Verdict.BLOCK:
                answer.attempts.append(
                    Attempt(index, sql, "plan", False, [f.message for f in gate.findings])
                )
                if index < budget:
                    feedback = (
                        "[PLAN_TOO_EXPENSIVE] The query planner estimates this query is "
                        f"too expensive to run: {gate.reasons()}. Narrow it -- add a "
                        "selective WHERE clause, aggregate instead of returning raw rows, "
                        "or restrict the time range."
                    )
                    previous_sql = sql
                    continue
                answer.status = "rejected"
                answer.sql = sql
                answer.error = f"Query plan rejected: {gate.reasons()}"
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer

            if gate.verdict is Verdict.WARN:
                answer.warnings.extend(f.message for f in gate.findings)

            answer.sql = sql
            answer.attempts.append(Attempt(index, sql, "plan", True))

            # ---------------- execute ----------------
            if not execute:
                answer.status = "ok"
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer

            exec_start = time.perf_counter()
            try:
                columns, rows = fetch_all(sql, settings=self.settings)
            except psycopg.errors.QueryCanceled:
                # The statement timeout fired. Not the model's fault in a way it can fix
                # reliably, and re-running it costs another timeout, so we stop.
                answer.status = "error"
                answer.error = (
                    f"Query exceeded the {self.settings.statement_timeout_ms} ms statement "
                    "timeout and was cancelled by the server."
                )
                answer.attempts.append(Attempt(index, sql, "execute", False, [answer.error]))
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer
            except psycopg.Error as err:
                message = str(err).strip().splitlines()[0]
                answer.attempts.append(Attempt(index, sql, "execute", False, [message]))
                if index < budget:
                    feedback = f"[SQL_ERROR] PostgreSQL rejected the query: {message}"
                    previous_sql = sql
                    continue
                answer.status = "error"
                answer.error = message
                answer.elapsed_ms = (time.perf_counter() - started) * 1000
                answer.generation_ms = generation_ms
                return answer

            answer.execution_ms = (time.perf_counter() - exec_start) * 1000
            answer.columns = columns
            answer.rows = rows
            answer.row_count = len(rows)
            answer.status = "ok"
            answer.attempts.append(Attempt(index, sql, "execute", True))
            answer.elapsed_ms = (time.perf_counter() - started) * 1000
            answer.generation_ms = generation_ms
            return answer

        answer.status = "error"
        answer.error = "retry budget exhausted"
        answer.elapsed_ms = (time.perf_counter() - started) * 1000
        return answer
