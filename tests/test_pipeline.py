"""Pipeline behaviour, driven by scripted providers rather than a live model."""

from __future__ import annotations

from nl2sql.llm import LLMResponse
from nl2sql.pipeline import Pipeline
from nl2sql.prompt import BuiltPrompt
from tests.conftest import needs_db


class Scripted:
    """Returns a fixed sequence of answers and records how often it was asked."""

    name = "scripted"

    def __init__(self, *answers: LLMResponse) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.feedback: list[str] = []

    def generate(self, prompt: BuiltPrompt) -> LLMResponse:
        if "Rejection reasons:" in prompt.user:
            self.feedback.append(prompt.user.split("Rejection reasons:")[1].strip())
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return answer


def sql_answer(sql: str) -> LLMResponse:
    return LLMResponse("sql", sql, None, "interpretation", [], provider="scripted")


@needs_db
def test_happy_path_executes_and_reports() -> None:
    provider = Scripted(sql_answer("SELECT count(*) AS n FROM courses LIMIT 1"))
    answer = Pipeline(provider=provider).answer("how many courses")  # type: ignore[arg-type]
    assert answer.status == "ok"
    assert answer.row_count == 1
    assert answer.retries == 0
    assert provider.calls == 1
    assert answer.plan_summary


@needs_db
def test_missing_limit_is_repaired_without_a_model_call() -> None:
    """A deterministic AST edit with one correct answer should not cost a round trip."""
    provider = Scripted(sql_answer("SELECT id FROM courses"))
    answer = Pipeline(provider=provider).answer("courses")  # type: ignore[arg-type]
    assert answer.status == "ok"
    assert provider.calls == 1, "repairing a missing LIMIT must not spend a retry"
    assert any("LIMIT" in w for w in answer.warnings)


@needs_db
def test_invalid_sql_is_retried_with_the_exact_error() -> None:
    provider = Scripted(
        sql_answer("SELECT nonexistent_column FROM courses LIMIT 5"),
        sql_answer("SELECT title FROM courses LIMIT 5"),
    )
    answer = Pipeline(provider=provider).answer("titles")  # type: ignore[arg-type]
    assert answer.status == "ok"
    assert provider.calls == 2
    assert answer.retries == 1
    assert "UNKNOWN_COLUMN" in provider.feedback[0]


@needs_db
def test_retry_budget_is_bounded() -> None:
    provider = Scripted(sql_answer("SELECT made_up FROM courses LIMIT 5"))
    answer = Pipeline(provider=provider).answer("x", max_retries=2)  # type: ignore[arg-type]
    assert answer.status == "rejected"
    assert provider.calls == 3, "max_retries=2 means 3 calls"


@needs_db
def test_a_write_is_rejected_and_never_retried() -> None:
    """Retrying a DELETE means asking the model to have another go at deleting."""
    provider = Scripted(sql_answer("DELETE FROM payments"))
    answer = Pipeline(provider=provider).answer("delete payments", max_retries=5)  # type: ignore[arg-type]
    assert answer.status == "rejected"
    assert provider.calls == 1
    assert "NOT_A_READ" in (answer.error or "")


@needs_db
def test_ruinous_plan_is_blocked_before_execution() -> None:
    hostile = (
        "SELECT count(*) FROM submissions a "
        "JOIN submissions b ON a.score = b.score AND a.enrollment_id <> b.enrollment_id "
        "LIMIT 10"
    )
    provider = Scripted(sql_answer(hostile))
    answer = Pipeline(provider=provider).answer("cross join", max_retries=0)  # type: ignore[arg-type]
    assert answer.status == "rejected"
    assert "plan" in (answer.error or "").lower() or "cost" in (answer.error or "").lower()
    assert answer.execution_ms == 0.0, "a blocked query must never have been executed"


@needs_db
def test_clarification_is_a_first_class_outcome() -> None:
    provider = Scripted(
        LLMResponse("clarify", None, "Do you mean by grade or by revenue?", "ambiguous", [])
    )
    answer = Pipeline(provider=provider).answer("who is best")  # type: ignore[arg-type]
    assert answer.status == "clarify"
    assert answer.clarification_question
    assert answer.sql is None
