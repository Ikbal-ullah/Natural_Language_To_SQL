"""Prompt assembly and few-shot retrieval."""

from __future__ import annotations

import pytest

from nl2sql.examples import BANK, select_examples, tokenize
from nl2sql.prompt import PROMPT_STRATEGIES, RESPONSE_SCHEMA, build_prompt
from nl2sql.schema import SCHEMA_FORMATS, Catalog
from tests.conftest import needs_db


def test_selection_is_deterministic() -> None:
    """An evaluation that silently reorders its own prompt is measuring noise."""
    first = [e.question for e in select_examples("revenue by month", 4)]
    second = [e.question for e in select_examples("revenue by month", 4)]
    assert first == second


def test_selection_finds_the_relevant_example() -> None:
    top = select_examples("total money earned each month", 1)[0]
    assert "revenue" in top.question.lower()
    top = select_examples("how many people never handed anything in", 1)[0]
    assert "never submitted" in top.question.lower()


@pytest.mark.parametrize("k", [0, 1, 4, 8])
def test_selection_returns_exactly_k(k: int) -> None:
    """A variable-length prompt makes the sweep's few-shot axis meaningless."""
    assert len(select_examples("anything at all", k)) == min(k, len(BANK))


def test_tokenizer_folds_plurals_and_drops_stop_words() -> None:
    assert "submission" in tokenize("submissions")
    assert "the" not in tokenize("the course")


@needs_db
@pytest.mark.parametrize("strategy", PROMPT_STRATEGIES)
def test_every_strategy_builds(strategy: str, catalog: Catalog) -> None:
    prompt = build_prompt("how many learners", catalog, strategy=strategy)  # type: ignore[arg-type]
    assert "learners" in prompt.system
    assert prompt.user.startswith("Question:")
    assert prompt.approx_tokens() > 100


@needs_db
@pytest.mark.parametrize("fmt", SCHEMA_FORMATS)
def test_every_schema_format_builds(fmt: str, catalog: Catalog) -> None:
    prompt = build_prompt("how many learners", catalog, schema_format=fmt)  # type: ignore[arg-type]
    assert "submissions" in prompt.system


@needs_db
def test_question_is_not_in_the_system_prompt(catalog: Catalog) -> None:
    """The cacheable prefix must not change per request.

    Putting the question in `system` is the most common and most expensive prompt-caching
    mistake: the cache never hits and the bill is roughly 10x what it needed to be.
    """
    marker = "zzz-unique-question-marker"
    prompt = build_prompt(marker, catalog)
    assert marker not in prompt.system
    assert marker in prompt.user


@needs_db
def test_two_questions_share_a_system_prefix(catalog: Catalog) -> None:
    a = build_prompt("how many learners", catalog, few_shot_count=0)
    b = build_prompt("total revenue", catalog, few_shot_count=0)
    assert a.system == b.system


@needs_db
def test_retry_feedback_carries_the_machine_error(catalog: Catalog) -> None:
    prompt = build_prompt(
        "x", catalog, error_feedback="[UNKNOWN_COLUMN] no such column", previous_sql="SELECT 1"
    )
    assert "[UNKNOWN_COLUMN]" in prompt.user
    assert "SELECT 1" in prompt.user


def test_response_schema_makes_clarifying_a_first_class_answer() -> None:
    action = RESPONSE_SCHEMA["properties"]["action"]  # type: ignore[index]
    assert set(action["enum"]) == {"sql", "clarify"}  # type: ignore[index]
    assert "interpretation" in RESPONSE_SCHEMA["required"]  # type: ignore[operator]


def test_validation_cannot_depend_on_the_prompt() -> None:
    """The architectural invariant behind "validation lives outside the prompt".

    Asserted structurally rather than by grepping the prompt text: the validator must not
    import the prompt or example modules, and `validate()` must have no parameter through
    which a question could reach it. If either of those ever changes, the separation that
    makes the validator a control rather than a preference has been broken.
    """
    import inspect

    from nl2sql import validator

    source = inspect.getsource(validator)
    assert "from nl2sql.prompt" not in source
    assert "from nl2sql.examples" not in source
    assert "question" not in set(inspect.signature(validator.validate).parameters)
