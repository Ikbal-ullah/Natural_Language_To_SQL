"""Schema introspection and serialisation."""

from __future__ import annotations

import pytest

from nl2sql.schema import SCHEMA_FORMATS, Catalog
from nl2sql.serialize import serialize
from tests.conftest import needs_db

EXPECTED_TABLES = {
    "learners", "courses", "cohorts", "enrollments",
    "assignments", "submissions", "mentor_sessions", "payments",
}


@needs_db
def test_all_eight_tables_are_present(catalog: Catalog) -> None:
    assert set(catalog.tables) == EXPECTED_TABLES


@needs_db
def test_bookkeeping_tables_are_hidden_from_the_model(catalog: Catalog) -> None:
    """schema_migrations exists in the database but must never reach the prompt."""
    assert "schema_migrations" not in catalog.tables
    assert "schema_migrations" not in serialize(catalog, "ddl")


@needs_db
def test_denormalised_column_is_protected_by_a_composite_fk(catalog: Catalog) -> None:
    """The one deliberate denormalisation must be enforced by the engine, not by hope."""
    fks = catalog.tables["enrollments"].foreign_keys
    composite = [fk for fk in fks if set(fk.columns) == {"cohort_id", "course_id"}]
    assert composite, "enrollments.course_id is not tied to cohorts.course_id by an FK"
    assert composite[0].ref_table == "cohorts"
    assert set(composite[0].ref_columns) == {"id", "course_id"}


@needs_db
def test_column_comments_reach_the_serialised_schema(catalog: Catalog) -> None:
    column = catalog.tables["enrollments"].column("course_id")
    assert column is not None and column.comment
    assert "DENORMALISED" in column.comment
    assert column.comment[:30] in serialize(catalog, "ddl")


@needs_db
def test_enum_values_are_exposed(catalog: Catalog) -> None:
    """Without the legal values the model invents 'ACTIVE' and silently returns nothing."""
    status = catalog.tables["enrollments"].column("status")
    assert status is not None
    assert status.enum_values == ("active", "completed", "dropped", "deferred")
    assert "enum(active|completed|dropped|deferred)" in serialize(catalog, "compact")


@needs_db
@pytest.mark.parametrize("fmt", SCHEMA_FORMATS)
def test_every_format_carries_the_same_facts(fmt: str, catalog: Catalog) -> None:
    text = serialize(catalog, fmt)  # type: ignore[arg-type]
    for table in EXPECTED_TABLES:
        assert table in text
    assert "learner_id" in text and "submitted_at" in text


@needs_db
def test_row_counts_are_in_the_prompt(catalog: Catalog) -> None:
    """A model told submissions has 5M rows writes different SQL from one told nothing."""
    assert catalog.tables["submissions"].row_estimate > 1_000_000
    assert "5,0" in serialize(catalog, "compact")


@needs_db
def test_sqlglot_schema_shape(catalog: Catalog) -> None:
    schema = catalog.sqlglot_schema()
    assert schema["learners"]["email"].upper().startswith("TEXT")
    assert set(schema) == EXPECTED_TABLES
