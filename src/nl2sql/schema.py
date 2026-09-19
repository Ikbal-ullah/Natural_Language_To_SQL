"""Schema introspection and serialisation.

The model cannot see the database. Everything it knows about the schema arrives as text
in the prompt, so the quality of that text is the single biggest lever on how often the
generated SQL is right -- bigger, in practice, than the choice of model.

Two commitments here:

1. THE CATALOG IS THE SOURCE OF TRUTH.  Tables, columns, types, foreign keys, CHECK
   constraints, enum values and human descriptions are all read out of PostgreSQL's own
   system catalogs at runtime. Nothing about the schema is retyped into Python. A prompt
   built from a hand-maintained dictionary drifts the first time someone adds a column,
   and a model reasoning about a schema that no longer exists produces SQL that is
   confidently, silently wrong.

2. SERIALISATION FORMAT IS A VARIABLE, NOT A DECISION.  There is no a-priori reason to
   believe DDL beats a compact one-liner or a markdown table. Each format costs a
   different number of tokens and conveys a different amount. So all four are implemented
   and eval/sweep.py measures them. See BENCHMARKS.md for which one actually won.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from nl2sql.config import Settings
from nl2sql.db import readonly_cursor

SchemaFormat = Literal["ddl", "compact", "markdown", "json"]
SCHEMA_FORMATS: tuple[SchemaFormat, ...] = ("ddl", "compact", "markdown", "json")


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    comment: str | None = None
    enum_values: tuple[str, ...] = ()
    is_primary_key: bool = False

    def type_label(self) -> str:
        """Enum columns are shown with their values: the model must not invent 'ACTIVE'."""
        if self.enum_values:
            return f"enum({'|'.join(self.enum_values)})"
        return self.data_type


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]

    def describe(self, table: str) -> str:
        left = ", ".join(f"{table}.{c}" for c in self.columns)
        right = ", ".join(f"{self.ref_table}.{c}" for c in self.ref_columns)
        return f"{left} -> {right}"


@dataclass(frozen=True)
class Table:
    name: str
    comment: str | None
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...]
    unique_constraints: tuple[tuple[str, ...], ...]
    check_constraints: tuple[str, ...]
    row_estimate: int

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


@dataclass(frozen=True)
class Catalog:
    """The whole public schema, as the validator and the prompt builder both see it."""

    tables: dict[str, Table] = field(default_factory=dict)

    # -- lookups used by the validator ------------------------------------
    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables

    def has_column(self, table: str, column: str) -> bool:
        t = self.tables.get(table.lower())
        return bool(t and column.lower() in t.column_names)

    def columns_anywhere(self, column: str) -> list[str]:
        """Tables containing a column of this name. Used to explain an unknown column."""
        return [t.name for t in self.tables.values() if column.lower() in t.column_names]

    def sqlglot_schema(self) -> dict[str, dict[str, str]]:
        """Shape sqlglot's `qualify` optimiser expects: {table: {column: type}}."""
        return {
            name: {c.name: c.data_type.upper() for c in t.columns}
            for name, t in self.tables.items()
        }

    def relationship_lines(self) -> list[str]:
        out = []
        for name in sorted(self.tables):
            for fk in self.tables[name].foreign_keys:
                out.append(fk.describe(name))
        return out


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------

_COLUMNS_SQL = """
SELECT
    c.relname                                   AS table_name,
    a.attname                                   AS column_name,
    format_type(a.atttypid, a.atttypmod)        AS data_type,
    NOT a.attnotnull                            AS nullable,
    col_description(c.oid, a.attnum)            AS comment,
    t.typtype                                   AS type_kind,
    CASE WHEN t.typtype = 'e' THEN (
        SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_enum e WHERE e.enumtypid = t.oid
    ) END                                       AS enum_values
FROM pg_attribute a
JOIN pg_class     c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type      t ON t.oid = a.atttypid
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_TABLES_SQL = """
SELECT c.relname AS table_name,
       obj_description(c.oid) AS comment,
       GREATEST(c.reltuples, 0)::bigint AS row_estimate
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname
"""

_CONSTRAINTS_SQL = """
SELECT
    rel.relname                     AS table_name,
    con.contype                     AS kind,
    con.conname                     AS name,
    pg_get_constraintdef(con.oid)   AS definition,
    ARRAY(
        SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        ORDER BY k.ord
    )                               AS cols,
    fref.relname                    AS ref_table,
    ARRAY(
        SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
        ORDER BY k.ord
    )                               AS ref_cols
FROM pg_constraint con
JOIN pg_class rel      ON rel.oid = con.conrelid
JOIN pg_namespace n    ON n.oid = rel.relnamespace
LEFT JOIN pg_class fref ON fref.oid = con.confrelid
WHERE n.nspname = 'public' AND con.contype IN ('p', 'f', 'u', 'c')
ORDER BY rel.relname, con.contype
"""

# Internal bookkeeping the model must never be told about: showing it a migrations table
# invites questions to be answered from it.
HIDDEN_TABLES = frozenset({"schema_migrations"})


def introspect(settings: Settings | None = None) -> Catalog:
    """Read the live schema out of PostgreSQL's catalogs."""
    with readonly_cursor(settings) as cur:
        cur.execute(_TABLES_SQL)
        table_meta = {r["table_name"]: r for r in cur.fetchall()}
        cur.execute(_COLUMNS_SQL)
        column_rows = cur.fetchall()
        cur.execute(_CONSTRAINTS_SQL)
        constraint_rows = cur.fetchall()

    pk_cols: dict[str, tuple[str, ...]] = {}
    fks: dict[str, list[ForeignKey]] = {}
    uniques: dict[str, list[tuple[str, ...]]] = {}
    checks: dict[str, list[str]] = {}
    for r in constraint_rows:
        table = r["table_name"]
        if table in HIDDEN_TABLES:
            continue
        kind = r["kind"]
        if kind == "p":
            pk_cols[table] = tuple(r["cols"])
        elif kind == "f":
            fks.setdefault(table, []).append(
                ForeignKey(tuple(r["cols"]), r["ref_table"], tuple(r["ref_cols"]))
            )
        elif kind == "u":
            uniques.setdefault(table, []).append(tuple(r["cols"]))
        elif kind == "c":
            checks.setdefault(table, []).append(r["definition"])

    cols_by_table: dict[str, list[Column]] = {}
    for r in column_rows:
        table = r["table_name"]
        if table in HIDDEN_TABLES:
            continue
        cols_by_table.setdefault(table, []).append(
            Column(
                name=r["column_name"],
                data_type=r["data_type"],
                nullable=bool(r["nullable"]),
                comment=r["comment"],
                enum_values=tuple(r["enum_values"] or ()),
                is_primary_key=r["column_name"] in pk_cols.get(table, ()),
            )
        )

    tables = {
        name: Table(
            name=name,
            comment=table_meta[name]["comment"],
            columns=tuple(cols),
            primary_key=pk_cols.get(name, ()),
            foreign_keys=tuple(fks.get(name, ())),
            unique_constraints=tuple(uniques.get(name, ())),
            check_constraints=tuple(checks.get(name, ())),
            row_estimate=int(table_meta[name]["row_estimate"]),
        )
        for name, cols in cols_by_table.items()
    }
    return Catalog(tables=tables)


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    """Process-wide cached catalog.

    Cached because introspection costs four catalog queries and the schema changes only
    when a migration runs -- i.e. never during a request. `refresh_catalog()` exists for
    tests and for the migration path.
    """
    return introspect()


def refresh_catalog() -> Catalog:
    get_catalog.cache_clear()
    return get_catalog()
