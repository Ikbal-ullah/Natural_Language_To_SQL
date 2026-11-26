"""Static validation of generated SQL, performed on the parsed AST.

WHY THIS FILE EXISTS AND THE PROMPT DOES NOT REPLACE IT
--------------------------------------------------------
The tempting design is to write "only generate SELECT statements, never modify data" in
the system prompt and call it a security control. It is not one, for a reason that has
nothing to do with how good the model is:

    The prompt is a request. The validator is a decision.

An attacker types into the same channel the instruction lives in. Anything the prompt
says, the user's text can argue with -- and it only has to win once. A control that fails
open under adversarial input is not a control; it is a preference. Worse, a prompt-based
rule has no audit trail: when it fails you get a DELETE and no record of a decision being
made, because no decision was ever made.

The validator is different in kind. It never reads the user's question. It reads the SQL,
parses it into a syntax tree, and answers a closed question about that tree's shape. There
is no sentence anyone can write that makes an `exp.Delete` node not be an `exp.Delete`
node.

WHY THE AST AND NOT THE STRING
------------------------------
Every string-level check is bypassable, and the bypasses are not exotic:

    "must start with SELECT"      ->  WITH d AS (DELETE FROM payments RETURNING *)
                                      SELECT * FROM d
                                      ...starts with WITH, and in PostgreSQL that CTE
                                      really does delete rows. sqlglot parses this to a
                                      Select at the root with a Delete inside it, so a
                                      root-node check passes it and a tree walk catches it.

    "must not contain 'drop'"     ->  matches the column name `dropped_at`, and misses
                                      /**/DROP/**/, DrOp, and DROP spelled across a
                                      newline.

    "must not contain a semicolon"->  misses `SELECT 1` followed by a comment-hidden
                                      second statement, and rejects a legitimate
                                      semicolon inside a string literal.

Parsing removes the entire class. Comments are gone, casing is normalised, string
literals are typed, and statement boundaries are structural facts rather than characters.
FAILURES.md #1 runs the string-matching version of this validator against the same
adversarial set and shows exactly which attacks walk through it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from nl2sql.config import Settings, get_settings
from nl2sql.schema import Catalog

DIALECT = "postgres"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Code(StrEnum):
    """Stable identifiers, so the evaluation harness can count failure types."""

    PARSE_ERROR = "PARSE_ERROR"
    EMPTY = "EMPTY"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_A_READ = "NOT_A_READ"
    WRITE_IN_SUBTREE = "WRITE_IN_SUBTREE"
    SELECT_INTO = "SELECT_INTO"
    LOCKING_CLAUSE = "LOCKING_CLAUSE"
    FORBIDDEN_FUNCTION = "FORBIDDEN_FUNCTION"
    SYSTEM_CATALOG = "SYSTEM_CATALOG"
    UNKNOWN_TABLE = "UNKNOWN_TABLE"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    AMBIGUOUS_COLUMN = "AMBIGUOUS_COLUMN"
    MISSING_LIMIT = "MISSING_LIMIT"
    LIMIT_TOO_LARGE = "LIMIT_TOO_LARGE"
    CROSS_JOIN = "CROSS_JOIN"


@dataclass(frozen=True)
class Issue:
    code: Code
    message: str
    severity: Severity = Severity.ERROR
    hint: str | None = None

    def render(self) -> str:
        return f"[{self.code.value}] {self.message}" + (f" Hint: {self.hint}" if self.hint else "")


@dataclass
class ValidationResult:
    ok: bool
    sql: str
    issues: list[Issue] = field(default_factory=list)
    ast: exp.Expression | None = None
    tables: tuple[str, ...] = ()

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    def feedback(self) -> str:
        """The text handed back to the model on a self-correction retry."""
        return "\n".join(i.render() for i in self.errors)


# ---------------------------------------------------------------------------
# denylists
# ---------------------------------------------------------------------------

# Every sqlglot node type that represents a write, a schema change, or a session command.
# Resolved with getattr because sqlglot renames nodes between major versions and a
# hardcoded attribute access would turn an upgrade into an AttributeError at import time
# -- i.e. into an outage -- rather than into a smaller denylist we can notice.
_WRITE_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Truncate", "Grant", "Revoke", "Command", "Transaction", "Commit",
    "Rollback", "Use", "Set", "SetItem", "Copy", "Refresh", "Analyze", "Vacuum",
    "Attach", "Detach", "Export", "Cache", "Uncache", "Call", "Prepare",
    # NOTE: exp.Fetch is deliberately absent -- `FETCH FIRST n ROWS ONLY` is standard
    # SQL for a LIMIT, and blocking it would reject valid read queries. Cursor FETCH
    # needs a DECLARE first, which is an exp.Command and is blocked.
    "Execute",
)
WRITE_NODES: tuple[type[exp.Expression], ...] = tuple(
    node
    for node in (getattr(exp, name, None) for name in _WRITE_NODE_NAMES)
    if isinstance(node, type) and issubclass(node, exp.Expression)
)

# Functions that read the filesystem, open outbound connections, execute further SQL, or
# consume server resources. None of them has a legitimate use in answering a question
# about learners and submissions, so the list is an allowlist boundary rather than a
# guess at what an attacker might try.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
        "pg_logdir_ls", "lo_import", "lo_export", "lo_get", "lo_put",
        "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
        "pg_sleep", "pg_sleep_for", "pg_sleep_until",
        "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
        "pg_rotate_logfile", "pg_create_restore_point",
        "set_config", "pg_switch_wal", "pg_promote",
        # query_to_xml and friends take a SQL string and run it -- a complete bypass of
        # every check in this file if they were allowed through.
        "query_to_xml", "query_to_xmlschema", "query_to_xml_and_xmlschema",
        "table_to_xml", "cursor_to_xml",
    }
)

# Anything in these namespaces is an administrative or filesystem/network primitive.
# An exact-name denylist (above) is bypassable by the next function PostgreSQL ships, e.g.
# pg_ls_waldir() or pg_read_server_files-era helpers, so whole prefixes are denied too.
FORBIDDEN_FUNCTION_PREFIXES = ("pg_", "dblink", "lo_", "pgp_", "xpath_", "txid_", "inet_")
FORBIDDEN_FUNCTIONS = FORBIDDEN_FUNCTIONS | frozenset(
    {"current_setting", "version", "current_database", "current_schema", "current_schemas"}
)

SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast", "pg_temp"})

# Set-returning functions that are legal in a FROM clause. `generate_series` in particular
# is how you build a date spine so that a weekly time series has rows for empty weeks.
ALLOWED_TABLE_FUNCTIONS = frozenset(
    {
        "generate_series", "unnest", "jsonb_array_elements", "json_array_elements",
        "regexp_split_to_table",
    }
)

_UNRESOLVED_COLUMN_RE = re.compile(r"Column '([^']+)' could not be resolved")


def _node_name(node: exp.Expression) -> str:
    return type(node).__name__


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def _check_single_statement(sql: str, issues: list[Issue]) -> list[exp.Expression] | None:
    try:
        # cast: sqlglot.parse is typed as returning its own Expr alias, which mypy does
        # not consider identical to exp.Expression even though it is.
        statements = cast(
            "list[exp.Expression]",
            [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None],
        )
    except ParseError as err:
        issues.append(
            Issue(
                Code.PARSE_ERROR,
                f"SQL does not parse: {str(err).splitlines()[0]}",
                hint="Return one valid PostgreSQL SELECT statement and nothing else.",
            )
        )
        return None
    if not statements:
        issues.append(Issue(Code.EMPTY, "No SQL statement found."))
        return None
    if len(statements) > 1:
        # Stacked statements are the classic injection shape. Note that we do not look
        # for a semicolon character: a semicolon inside a string literal is harmless and
        # a second statement can be introduced without one being visible in a naive scan.
        issues.append(
            Issue(
                Code.MULTIPLE_STATEMENTS,
                f"Expected exactly one statement, found {len(statements)}.",
                hint="Return a single SELECT.",
            )
        )
        return None
    return statements


def _check_read_only(tree: exp.Expression, issues: list[Issue]) -> None:
    """The root must be a read, AND no write node may appear anywhere in the tree."""
    read_roots = (
        exp.Select, exp.Union, exp.Subquery, exp.With, exp.Except, exp.Intersect,
    )
    if not isinstance(tree, read_roots):
        issues.append(
            Issue(
                Code.NOT_A_READ,
                f"Statement is a {_node_name(tree)}, not a query.",
                hint="Only SELECT statements are permitted.",
            )
        )
        return

    for node in tree.walk():
        if isinstance(node, WRITE_NODES):
            # This is the check that catches
            #   WITH d AS (DELETE FROM payments RETURNING *) SELECT * FROM d
            # whose root node is a perfectly innocent Select.
            issues.append(
                Issue(
                    Code.WRITE_IN_SUBTREE,
                    f"Statement contains a {_node_name(node).upper()} node. "
                    "The query is read-only or it does not run.",
                    hint="Rewrite as a plain SELECT with no data-modifying CTE.",
                )
            )
            return

    if tree.find(exp.Into) is not None:
        # SELECT ... INTO new_table is DDL wearing a SELECT costume.
        issues.append(
            Issue(
                Code.SELECT_INTO,
                "SELECT ... INTO creates a new table and is not a read.",
                hint="Remove the INTO clause.",
            )
        )

    for select in tree.find_all(exp.Select):
        if select.args.get("locks"):
            issues.append(
                Issue(
                    Code.LOCKING_CLAUSE,
                    "Locking clauses (FOR UPDATE / FOR SHARE) take row locks and are not reads.",
                    hint="Remove the FOR UPDATE / FOR SHARE clause.",
                )
            )
            break


def _check_functions(tree: exp.Expression, issues: list[Issue]) -> None:
    for node in tree.find_all(exp.Func):
        name = (node.sql_name() if hasattr(node, "sql_name") else type(node).__name__).lower()
        if isinstance(node, exp.Anonymous):
            name = str(node.this).lower()
        if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            issues.append(
                Issue(
                    Code.FORBIDDEN_FUNCTION,
                    f"Function {name}() is not permitted: it reads files, opens "
                    "connections, executes further SQL or consumes server resources.",
                    hint="Answer the question using only the application tables.",
                )
            )


def _table_key(table: exp.Table) -> tuple[str, str]:
    return (str(table.db or "").lower(), str(table.name or "").lower())


def _cte_names(tree: exp.Expression) -> set[str]:
    return {str(cte.alias_or_name).lower() for cte in tree.find_all(exp.CTE)}


def _check_tables(
    tree: exp.Expression, catalog: Catalog, issues: list[Issue]
) -> tuple[str, ...]:
    """Every table must exist in the catalog, be a CTE defined here, or be allowlisted."""
    local = _cte_names(tree)
    referenced: list[str] = []
    for table in tree.find_all(exp.Table):
        schema_name, name = _table_key(table)
        if not name:
            continue
        if schema_name in SYSTEM_SCHEMAS or name.startswith("pg_"):
            issues.append(
                Issue(
                    Code.SYSTEM_CATALOG,
                    f"Access to system catalog {schema_name or 'pg_catalog'}.{name} is denied.",
                    hint="Only the application tables may be queried.",
                )
            )
            continue
        if name in local or name in ALLOWED_TABLE_FUNCTIONS:
            continue
        if schema_name not in ("", "public"):
            issues.append(
                Issue(
                    Code.UNKNOWN_TABLE,
                    f"Schema {schema_name!r} is not accessible.",
                    hint="Query tables without a schema prefix.",
                )
            )
            continue
        if not catalog.has_table(name):
            issues.append(
                Issue(
                    Code.UNKNOWN_TABLE,
                    f"Table {name!r} does not exist.",
                    hint=f"Available tables: {', '.join(sorted(catalog.tables))}.",
                )
            )
            continue
        referenced.append(name)
    return tuple(sorted(set(referenced)))


def _check_columns(tree: exp.Expression, catalog: Catalog, issues: list[Issue]) -> None:
    """Resolve every column against the schema using sqlglot's scope analyser.

    Doing this by hand is harder than it looks: a column reference is legal if it resolves
    in ANY enclosing scope, aliases shadow table names, CTEs introduce new named sources, and a
    correlated subquery can see its parent's tables. `qualify` already implements those
    scoping rules, so we lean on it and translate its errors instead of reimplementing
    name resolution badly.
    """
    if any(i.code in (Code.UNKNOWN_TABLE, Code.SYSTEM_CATALOG) for i in issues):
        # Column resolution against a schema we already know is wrong produces cascading
        # noise; the table error is the useful message.
        return
    try:
        # cast: sqlglot types `schema` as dict[str, object], and dict is invariant, so a
        # dict[str, dict[str, str]] is rejected despite being exactly what it wants.
        sqlglot_schema = cast("dict[str, object]", catalog.sqlglot_schema())
        qualify(
            tree.copy(),
            schema=sqlglot_schema,
            dialect=DIALECT,
            validate_qualify_columns=True,
            infer_schema=True,
        )
    except OptimizeError as err:
        text = str(err)
        match = _UNRESOLVED_COLUMN_RE.search(text)
        if not match:
            issues.append(Issue(Code.UNKNOWN_COLUMN, f"Column resolution failed: {text}"))
            return
        column = match.group(1)
        owners = catalog.columns_anywhere(column)
        if owners:
            issues.append(
                Issue(
                    Code.AMBIGUOUS_COLUMN,
                    f"Column {column!r} is ambiguous or not in scope.",
                    hint=f"Qualify it with a table alias; it exists on: {', '.join(owners)}.",
                )
            )
        else:
            issues.append(
                Issue(
                    Code.UNKNOWN_COLUMN,
                    f"Column {column!r} does not exist on any table in scope.",
                    hint="Use only columns listed in the schema.",
                )
            )
    except Exception as err:  # pragma: no cover - sqlglot internal edge cases
        # A crash inside the optimiser must not be reported as "valid". Fail closed.
        issues.append(
            Issue(
                Code.UNKNOWN_COLUMN,
                f"Could not verify columns ({type(err).__name__}: {err}).",
                hint="Simplify the query; qualify every column with its table alias.",
            )
        )


def _is_cross_join(join: exp.Join) -> bool:
    """True for CROSS JOIN, a comma join, or a JOIN whose ON clause is a constant true."""
    if join.args.get("using"):
        return False
    on = join.args.get("on")
    kind = str(join.args.get("kind") or "").upper()
    side = join.args.get("side")
    if kind == "CROSS":
        return True
    if on is None:
        return not side and not kind  # bare `FROM a, b` / `JOIN b` with no condition
    if isinstance(on, exp.Boolean) and on.this is True:
        return True
    return (
        isinstance(on, exp.EQ)
        and isinstance(on.this, exp.Literal)
        and isinstance(on.expression, exp.Literal)
        and on.this.name == on.expression.name  # ON 1 = 1
    )


def _has_column_equality(select: exp.Select) -> bool:
    where = select.args.get("where")
    if where is None:
        return False
    return any(
        isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column)
        for eq in where.find_all(exp.EQ)
    )


def _check_cross_joins(tree: exp.Expression, issues: list[Issue]) -> None:
    """Reject a join of two real tables with no join condition (Cartesian product).

    A CROSS JOIN between orders (10k rows) and order_items (25k rows) is 250 million rows
    and no LIMIT saves you: the planner still has to produce the product before sorting
    or aggregating it. Joining against a CTE, subquery, LATERAL or set-returning function
    (the "one-row totals" pattern, date spines) is allowed: those are bounded by
    construction.
    """
    local = _cte_names(tree)
    for join in tree.find_all(exp.Join):
        if not _is_cross_join(join):
            continue
        right = join.this
        if not isinstance(right, exp.Table):
            continue  # subquery / lateral / table function: bounded by construction
        name = str(right.name or "").lower()
        if name in local or name in ALLOWED_TABLE_FUNCTIONS or isinstance(right.this, exp.Func):
            continue
        select = join.parent
        explicit_cross = str(join.args.get("kind") or "").upper() == "CROSS"
        if (
            not explicit_cross
            and join.args.get("on") is None
            and isinstance(select, exp.Select)
            and _has_column_equality(select)
        ):
            continue  # legacy comma join whose WHERE supplies the join condition
        issues.append(
            Issue(
                Code.CROSS_JOIN,
                f"Unbounded Cartesian product: table {name!r} is joined without a join condition.",
                hint="Use JOIN ... ON <fk> = <pk> following the foreign keys.",
            )
        )
        return


def is_scalar_query(tree: exp.Expression) -> bool:
    """True when the query can only ever return one row.

    A LIMIT is for bounding a result that can grow; `SELECT count(*) FROM orders` cannot.
    The shape test: a plain SELECT, no GROUP BY / DISTINCT / window, and every projected
    column reference sits inside an aggregate (or there is no FROM at all).
    """
    node = tree.this if isinstance(tree, exp.Subquery) and tree.this is not None else tree
    if not isinstance(node, exp.Select):
        return False
    if node.args.get("group") or node.args.get("distinct") or node.args.get("having"):
        return False
    projections = node.expressions
    if not projections:
        return False
    for proj in projections:
        if any(True for _ in proj.find_all(exp.Window, exp.UDTF, exp.Unnest)):
            return False
        has_agg = proj.find(exp.AggFunc) is not None
        for col in proj.find_all(exp.Column):
            if col.find_ancestor(exp.AggFunc) is None:
                return False
        if not has_agg and node.args.get("from") is not None:
            return False
    return True


def _limit_value(tree: exp.Expression) -> int | None:
    """Extract the row bound from the outermost query.

    PostgreSQL spells this two ways -- `LIMIT n` and the SQL-standard
    `FETCH FIRST n ROWS ONLY` -- and sqlglot models them as different nodes. Handling only
    the first would reject valid bounded SQL, which trains the model (via the retry loop)
    to avoid standard syntax for no reason.
    """
    node: exp.Expression | None = tree
    if isinstance(tree, exp.Subquery) and tree.this is not None:
        node = tree.this
    if node is None:
        return None

    limit = node.args.get("limit")
    if limit is None:
        limit = node.args.get("fetch")
    if limit is None:
        return None

    if isinstance(limit, exp.Fetch):
        expression: Any = limit.args.get("count")
    elif isinstance(limit, exp.Limit):
        expression = limit.expression
    else:
        expression = limit
    if expression is None:
        return None
    try:
        return int(expression.name if hasattr(expression, "name") else expression)
    except (AttributeError, ValueError, TypeError):
        return None


def _check_limit(tree: exp.Expression, settings: Settings, issues: list[Issue]) -> None:
    """A generated query must be bounded before it runs.

    Not because a large result is wrong, but because nobody -- not the model, not the
    planner -- knows how many rows a question returns until it has already produced them.
    An unbounded query that matches 5,000,000 rows serialises all of them into the API
    process's memory. The LIMIT is what makes "the model got it wrong" cost 3 ms instead
    of an out-of-memory kill. FAILURES.md #2 measures exactly that difference.
    """
    value = _limit_value(tree)
    if value is None and is_scalar_query(tree):
        return  # a one-row aggregate needs no bound
    if value is None:
        issues.append(
            Issue(
                Code.MISSING_LIMIT,
                "Query has no LIMIT clause.",
                hint=f"Append LIMIT {settings.default_limit}.",
            )
        )
        return
    if value > settings.max_limit:
        issues.append(
            Issue(
                Code.LIMIT_TOO_LARGE,
                f"LIMIT {value} exceeds the maximum of {settings.max_limit}.",
                hint=f"Use LIMIT {settings.max_limit} or less.",
            )
        )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def validate(
    sql: str,
    catalog: Catalog,
    settings: Settings | None = None,
) -> ValidationResult:
    """Validate generated SQL. Never sees the user's question -- only the SQL.

    That separation is deliberate and is the property that makes this a control: there is
    no input path by which a cleverly worded question can influence the verdict.
    """
    settings = settings or get_settings()
    issues: list[Issue] = []

    if not sql or not sql.strip():
        return ValidationResult(ok=False, sql=sql, issues=[Issue(Code.EMPTY, "Empty SQL.")])

    statements = _check_single_statement(sql, issues)
    if statements is None:
        return ValidationResult(ok=False, sql=sql, issues=issues)

    tree = statements[0]
    _check_read_only(tree, issues)
    if any(i.code in (Code.NOT_A_READ, Code.WRITE_IN_SUBTREE) for i in issues):
        # Stop here: nothing below this line is meaningful for a statement that is not a
        # read, and reporting six further problems buries the one that matters.
        return ValidationResult(ok=False, sql=sql, issues=issues, ast=tree)

    _check_functions(tree, issues)
    _check_cross_joins(tree, issues)
    tables = _check_tables(tree, catalog, issues)
    _check_columns(tree, catalog, issues)
    _check_limit(tree, settings, issues)

    ok = not any(i.severity is Severity.ERROR for i in issues)
    return ValidationResult(ok=ok, sql=sql, issues=issues, ast=tree, tables=tables)


def add_limit(sql: str, limit: int) -> str:
    """Deterministically append a LIMIT by editing the AST.

    Used by the pipeline to repair a missing LIMIT without spending a model call. Done on
    the tree rather than by string concatenation, because `sql + ' LIMIT 100'` produces
    invalid SQL the moment the query ends in a comment, a semicolon, or already has a
    LIMIT deeper in a subquery.
    """
    tree = sqlglot.parse_one(sql, dialect=DIALECT)
    if not isinstance(tree, exp.Query):
        raise ValueError(f"cannot add a LIMIT to a {type(tree).__name__}")
    return tree.limit(limit).sql(dialect=DIALECT, pretty=True)
