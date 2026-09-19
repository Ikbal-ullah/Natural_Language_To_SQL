"""The safety-critical tests. If these fail, nothing else matters."""

from __future__ import annotations

import pytest

from eval.adversarial import ADVERSARIAL_CASES
from nl2sql.schema import Catalog
from nl2sql.validator import Code, add_limit, validate
from tests.conftest import needs_db

VALID = [
    "SELECT id, email FROM learners WHERE status = 'active' LIMIT 10",
    "SELECT c.title, count(*) AS n FROM submissions s "
    "JOIN enrollments e ON e.id = s.enrollment_id JOIN courses c ON c.id = e.course_id "
    "GROUP BY c.title HAVING count(*) > 5 ORDER BY n DESC LIMIT 10",
    "WITH r AS (SELECT learner_id, rank() OVER (ORDER BY final_grade DESC) rk "
    "FROM enrollments) SELECT * FROM r WHERE rk <= 3 LIMIT 20",
    "SELECT count(*) FILTER (WHERE status = 'graded') FROM submissions LIMIT 1",
    "SELECT id FROM learners ORDER BY id FETCH FIRST 10 ROWS ONLY",
    "SELECT 1 /* DROP TABLE payments */ LIMIT 1",
    "SELECT g.d::date FROM generate_series('2024-01-01'::date, '2024-02-01'::date, "
    "'1 week') g(d) LIMIT 10",
    "SELECT l.id FROM learners l WHERE EXISTS "
    "(SELECT 1 FROM enrollments e WHERE e.learner_id = l.id) LIMIT 10",
]


@needs_db
@pytest.mark.parametrize("sql", VALID)
def test_valid_sql_is_accepted(sql: str, catalog: Catalog) -> None:
    result = validate(sql, catalog)
    assert result.ok, result.feedback()


@needs_db
@pytest.mark.parametrize("case", ADVERSARIAL_CASES, ids=lambda c: c.id)
def test_every_adversarial_payload_is_blocked(case, catalog: Catalog) -> None:  # type: ignore[no-untyped-def]
    """The headline safety claim, asserted per case rather than as an aggregate.

    Note this test hands the payload straight to the validator: no model is involved, so
    a pass cannot be explained by the model having been well behaved.
    """
    result = validate(case.payload_sql, catalog)
    assert not result.ok, f"{case.id} ({case.technique}) was ALLOWED"
    fired = {i.code.value for i in result.errors}
    assert fired & set(case.expected_codes), (
        f"{case.id} was blocked, but by {fired} rather than the expected "
        f"{case.expected_codes} -- blocked for the wrong reason is not a pass"
    )


@needs_db
def test_data_modifying_cte_is_not_fooled_by_the_root_node(catalog: Catalog) -> None:
    sql = "WITH d AS (DELETE FROM payments RETURNING *) SELECT * FROM d LIMIT 10"
    assert sql.lstrip().upper().startswith("WITH")  # a string check would pass this
    result = validate(sql, catalog)
    assert Code.WRITE_IN_SUBTREE in {i.code for i in result.errors}


@needs_db
def test_writes_short_circuit_other_checks(catalog: Catalog) -> None:
    result = validate("DELETE FROM payments", catalog)
    assert [i.code for i in result.errors] == [Code.NOT_A_READ]


@needs_db
def test_validator_never_sees_the_question(catalog: Catalog) -> None:
    """validate() takes SQL and a catalog. There is no parameter through which a
    persuasive question could reach it."""
    import inspect

    params = set(inspect.signature(validate).parameters)
    assert params == {"sql", "catalog", "settings"}


@needs_db
def test_add_limit_is_an_ast_edit(catalog: Catalog) -> None:
    repaired = add_limit("SELECT id FROM learners", 100)
    assert validate(repaired, catalog).ok
    # Idempotent, and does not produce `... LIMIT 5 LIMIT 100`.
    assert add_limit("SELECT id FROM learners LIMIT 5", 100).count("LIMIT") == 1
