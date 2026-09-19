"""Tests of the evaluation set itself.

An evaluation set is code, and a broken one is worse than none: it reports a number that
nobody checks. These are the checks.
"""

from __future__ import annotations

import pytest

from eval.adversarial import ADVERSARIAL_CASES
from eval.golden_set import CATEGORIES, GOLDEN_SET
from nl2sql.db import fetch_all
from nl2sql.examples import BANK, tokenize
from nl2sql.schema import Catalog
from nl2sql.validator import validate
from tests.conftest import needs_db

ANSWERABLE = [q for q in GOLDEN_SET if q.sql is not None]


def test_sixty_questions_across_six_categories() -> None:
    assert len(GOLDEN_SET) == 60
    assert {q.category for q in GOLDEN_SET} == set(CATEGORIES)
    assert len([q for q in GOLDEN_SET if q.expects_clarification]) == 10


def test_ids_are_unique() -> None:
    ids = [q.id for q in GOLDEN_SET]
    assert len(ids) == len(set(ids))


def test_ambiguous_questions_have_no_reference_sql() -> None:
    """Writing one would assert a reading the question does not support."""
    for q in GOLDEN_SET:
        if q.expects_clarification:
            assert q.sql is None
            assert q.ambiguity, f"{q.id} must say what the ambiguity actually is"


def test_no_contamination_between_few_shot_bank_and_golden_set() -> None:
    """If a golden question is also a prompt example, the score measures copying."""
    gold_exact = {q.question.strip().lower().rstrip(".?") for q in GOLDEN_SET}
    bank_exact = {e.question.strip().lower().rstrip(".?") for e in BANK}
    assert not (gold_exact & bank_exact)

    gold_tokens = {frozenset(tokenize(q.question)) for q in GOLDEN_SET}
    bank_tokens = {frozenset(tokenize(e.question)) for e in BANK}
    assert not (gold_tokens & bank_tokens), "a question differs only by wording"


@needs_db
@pytest.mark.parametrize("q", ANSWERABLE, ids=lambda q: q.id)
def test_reference_sql_is_valid_and_returns_rows(q, catalog: Catalog) -> None:  # type: ignore[no-untyped-def]
    result = validate(q.sql, catalog)
    assert result.ok, f"{q.id} reference SQL is invalid: {result.feedback()}"
    _, rows = fetch_all(q.sql)
    assert rows, (
        f"{q.id} reference returns zero rows -- a question whose correct answer is empty "
        "is passed by any broken query that also returns nothing"
    )


@needs_db
@pytest.mark.parametrize("q", ANSWERABLE, ids=lambda q: q.id)
def test_reference_sql_is_not_all_null(q) -> None:  # type: ignore[no-untyped-def]
    _, rows = fetch_all(q.sql)
    assert not (len(rows) == 1 and all(v is None for v in rows[0])), (
        f"{q.id} returns a single all-NULL row, which is effectively an empty answer"
    )


def test_fifteen_adversarial_cases_with_stated_expectations() -> None:
    assert len(ADVERSARIAL_CASES) == 15
    assert len({c.id for c in ADVERSARIAL_CASES}) == 15
    for case in ADVERSARIAL_CASES:
        assert case.expected_codes, f"{case.id} must say which check should fire"
        assert case.payload_sql.strip(), f"{case.id} needs a payload to test the validator"


def test_adversarial_techniques_are_varied() -> None:
    """Fifteen variations of DROP TABLE would be one test reported as fifteen."""
    assert len({c.technique for c in ADVERSARIAL_CASES}) >= 10


@needs_db
@pytest.mark.parametrize("example", BANK, ids=lambda e: e.question[:30])
def test_every_few_shot_example_actually_runs(example, catalog: Catalog) -> None:  # type: ignore[no-untyped-def]
    """A broken example teaches the model broken SQL on every request forever."""
    assert validate(example.sql, catalog).ok, f"invalid example: {example.question}"
    fetch_all(example.sql)
