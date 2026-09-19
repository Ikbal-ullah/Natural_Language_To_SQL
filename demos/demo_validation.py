"""An adversarial query blocked by the validator, with the parse tree that catches it.

Run: python demos/demo_validation.py
"""

import _bootstrap  # noqa: F401
import sqlglot
from sqlglot import exp

from eval.adversarial import ADVERSARIAL_CASES
from nl2sql.schema import get_catalog
from nl2sql.validator import validate

VERDICT = """
The root really is a SELECT and the statement really does delete rows: a PostgreSQL CTE may
contain DELETE ... RETURNING. Every check that reads the SQL as TEXT passes it. Walking the
tree finds the Delete node wherever it hides, and no phrasing changes that node's type."""
NAIVE = {  # checks people actually ship; each is defeated by the payload below
    "starts with SELECT/WITH": lambda s: s.lstrip().upper().startswith(("SELECT", "WITH")),
    "contains no 'DROP'": lambda s: "drop" not in s.lower(),
    "contains no semicolon": lambda s: ";" not in s,
}

def tree_lines(node: exp.Expression, depth: int = 0) -> list[str]:
    """Render the AST, marking any node that is a write."""
    writes = exp.Delete | exp.Insert | exp.Update
    danger = "   <== WRITE NODE" if isinstance(node, writes) else ""
    detail = f" ({node.name})" if isinstance(node, exp.Table) else (
        f" (alias={node.alias_or_name})" if isinstance(node, exp.CTE) else "")
    lines = [f"  {'  ' * depth}{type(node).__name__}{detail}{danger}"]
    for child in node.args.values():
        for item in child if isinstance(child, list) else [child]:
            if isinstance(item, exp.Expression):
                lines.extend(tree_lines(item, depth + 1))
    return lines

def main() -> int:
    case = next(c for c in ADVERSARIAL_CASES if c.id == "adv04")
    print(f"USER ASKS:\n  {case.prompt}\n")
    print(f"MODEL EMITS:\n  {case.payload_sql}\n")
    print("NAIVE STRING CHECKS -- what a prompt-plus-regex 'guard' would decide:")
    for name, check in NAIVE.items():
        ok = check(case.payload_sql)
        print(f"  {'PASSES ' if ok else 'blocks '} {name}"
              + ("   <== attack gets through" if ok else ""))
    tree = sqlglot.parse_one(case.payload_sql, dialect="postgres")
    print(f"\nPARSED AST (root is {type(tree).__name__} -- why every check above passes):")
    print("\n".join(tree_lines(tree)[:16]))

    result = validate(case.payload_sql, get_catalog())
    print(f"\nVALIDATOR VERDICT: {'ALLOWED' if result.ok else 'BLOCKED'}")
    for issue in result.errors:
        print(f"  {issue.render()}")
    print(VERDICT)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
