"""Four ways to describe the same schema to a model, so the sweep can measure which wins.

Every format carries the same facts: tables, columns, types, nullability, enum values,
primary keys, foreign keys and human descriptions. They differ only in shape and in how
many tokens they cost. That is exactly what makes them a fair comparison -- if one format
also carried extra facts, a win would tell you nothing about the format.

Rules of thumb this file encodes, all of which cost accuracy when broken:
  * Enum values must be listed. Otherwise the model writes status = 'ACTIVE' or
    status = 'complete' and gets zero rows with no error -- the worst failure mode there
    is, because it looks like a valid answer.
  * Foreign keys must be explicit and directional. Join paths are the thing models get
    wrong most often on a 3+ table question.
  * Row counts must be included. A model told that `submissions` has 5,000,000 rows and
    `courses` has 40 writes very different SQL from one told nothing.
  * Descriptions belong next to the column, not in a separate glossary at the bottom.
"""

from __future__ import annotations

import json

from nl2sql.schema import Catalog, SchemaFormat, Table

# Written into every prompt regardless of format: facts about this specific schema that
# are not deducible from the DDL and that we know from experience the model gets wrong.
SCHEMA_NOTES = """\
Notes that are not visible in the DDL:
- enrollments.course_id is a maintained duplicate of cohorts.course_id. Join
  enrollments.course_id directly to courses.id; going via cohorts is correct but slower.
- submissions is the only large table (~5,000,000 rows). Always filter or aggregate it;
  never select raw rows from it without a narrow WHERE clause.
- Money is integer cents. Divide by 100.0 to report dollars.
- Only payments with status = 'succeeded' (plus 'refunded' for net figures) represent
  money that actually moved. Revenue questions must filter on status.
- A submission's score is out of assignments.max_score, which is 20 for quizzes and 100
  otherwise. Percentages need the join to assignments.
- The latest attempt at an assignment is the row with the highest attempt_no for an
  (enrollment_id, assignment_id) pair.
- Mentors are rows in learners with is_mentor = true, referenced by
  mentor_sessions.mentor_id and cohorts.mentor_id."""


def _col_line(table: Table, indent: str = "  ") -> list[str]:
    lines = []
    for c in table.columns:
        bits = [f"{indent}{c.name} {c.type_label()}"]
        if not c.nullable:
            bits.append("NOT NULL")
        if c.is_primary_key:
            bits.append("PRIMARY KEY" if len(table.primary_key) == 1 else "PK")
        line = " ".join(bits)
        if c.comment:
            line = f"{line}  -- {c.comment}"
        lines.append(line)
    return lines


def to_ddl(catalog: Catalog) -> str:
    """CREATE TABLE form. The format a model has seen most often during training."""
    blocks = []
    for name in sorted(catalog.tables):
        t = catalog.tables[name]
        head = f"-- {t.comment}" if t.comment else None
        body = [f"CREATE TABLE {name} ("]
        body.extend(f"{line}," for line in _col_line(t))
        for fk in t.foreign_keys:
            cols = ", ".join(fk.columns)
            ref = ", ".join(fk.ref_columns)
            body.append(f"  FOREIGN KEY ({cols}) REFERENCES {fk.ref_table} ({ref}),")
        for uq in t.unique_constraints:
            body.append(f"  UNIQUE ({', '.join(uq)}),")
        body[-1] = body[-1].rstrip(",")
        body.append(f");  -- ~{t.row_estimate:,} rows")
        blocks.append("\n".join(([head] if head else []) + body))
    return "\n\n".join(blocks)


def to_compact(catalog: Catalog) -> str:
    """One line per table plus a relationship block. Roughly a third of the DDL tokens."""
    lines = []
    for name in sorted(catalog.tables):
        t = catalog.tables[name]
        cols = ", ".join(
            f"{c.name}:{c.type_label()}{'' if not c.is_primary_key else '(pk)'}"
            f"{'?' if c.nullable else ''}"
            for c in t.columns
        )
        lines.append(f"{name}[~{t.row_estimate:,}]({cols})")
    rel = catalog.relationship_lines()
    return "\n".join(lines) + "\n\nRelationships:\n" + "\n".join(f"  {r}" for r in rel)


def to_markdown(catalog: Catalog) -> str:
    """Markdown tables. Most readable to a human reviewing the prompt."""
    out = []
    for name in sorted(catalog.tables):
        t = catalog.tables[name]
        out.append(f"### {name}  (~{t.row_estimate:,} rows)")
        if t.comment:
            out.append(f"_{t.comment}_")
        out.append("")
        out.append("| column | type | null | description |")
        out.append("| --- | --- | --- | --- |")
        for c in t.columns:
            key = " 🔑" if c.is_primary_key else ""
            out.append(
                f"| {c.name}{key} | {c.type_label()} | "
                f"{'yes' if c.nullable else 'no'} | {c.comment or ''} |"
            )
        if t.foreign_keys:
            out.append("")
            for fk in t.foreign_keys:
                out.append(f"- FK: {fk.describe(name)}")
        out.append("")
    return "\n".join(out)


def to_json(catalog: Catalog) -> str:
    """Structured JSON. Unambiguous to parse, but the most token-expensive by far."""
    payload = {
        "tables": [
            {
                "name": name,
                "description": t.comment,
                "approx_rows": t.row_estimate,
                "primary_key": list(t.primary_key),
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type_label(),
                        "nullable": c.nullable,
                        "description": c.comment,
                    }
                    for c in t.columns
                ],
                "foreign_keys": [
                    {
                        "columns": list(fk.columns),
                        "references": {
                            "table": fk.ref_table,
                            "columns": list(fk.ref_columns),
                        },
                    }
                    for fk in t.foreign_keys
                ],
            }
            for name, t in sorted(catalog.tables.items())
        ]
    }
    return json.dumps(payload, indent=1)


_RENDERERS = {
    "ddl": to_ddl,
    "compact": to_compact,
    "markdown": to_markdown,
    "json": to_json,
}


def serialize(catalog: Catalog, fmt: SchemaFormat = "ddl", *, notes: bool = True) -> str:
    """Render the catalog in the requested format."""
    if fmt not in _RENDERERS:
        raise ValueError(f"unknown schema format {fmt!r}; expected one of {sorted(_RENDERERS)}")
    body = _RENDERERS[fmt](catalog)
    return f"{body}\n\n{SCHEMA_NOTES}" if notes else body
