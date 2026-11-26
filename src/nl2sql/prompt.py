"""Prompt construction.

Three strategies are implemented so eval/sweep.py can measure them rather than us
asserting which is best:

  minimal     schema + question. The honest baseline. If the elaborate prompt does not
              beat this, the elaborate prompt is decoration.
  structured  schema + domain notes + explicit rules + retrieved few-shot examples.
  cot         structured, plus an instruction to state the join path and the filters
              before writing SQL. On a model with adaptive thinking this is partly
              redundant, which is itself worth measuring rather than assuming.

THE OUTPUT CONTRACT
-------------------
The model returns JSON against a fixed schema, not prose containing a code fence. Two
reasons:

  1. Parsing prose is a failure mode. "Here is the SQL:" wrapped in ``` is usually fine
     and occasionally is not, and the occasional case shows up as a mysterious accuracy
     drop rather than as an error.
  2. Refusing to answer must be a first-class outcome. Ten of the sixty evaluation
     questions are ambiguous on purpose, and the correct behaviour is to ask a
     clarifying question rather than to guess. If the only channel is "return SQL", the
     model will always return SQL, because that is the only thing it can do. Giving
     `action: "clarify"` equal standing in the schema makes not-answering a normal
     result instead of a failure.

Note what is NOT in the prompt: any claim that the prompt is a security boundary. The
rules below ask for read-only SQL because that produces better SQL, not because asking
is what stops a DELETE. What stops a DELETE is validator.py and migration 0008.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nl2sql.examples import FewShotExample, select_examples
from nl2sql.schema import Catalog, SchemaFormat
from nl2sql.serialize import serialize

PromptStrategy = Literal["minimal", "structured", "cot"]
PROMPT_STRATEGIES: tuple[PromptStrategy, ...] = ("minimal", "structured", "cot")

# The JSON schema the model must fill in. Kept small: every optional field is a field the
# model can get wrong.
RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["sql", "clarify"],
            "description": (
                "'sql' when the question has one clear answer. 'clarify' when the "
                "question is genuinely ambiguous and a reasonable analyst would have to "
                "ask before answering."
            ),
        },
        "sql": {
            "type": ["string", "null"],
            "description": "A single read-only PostgreSQL SELECT with a LIMIT. Null when "
            "action is 'clarify'.",
        },
        "clarification_question": {
            "type": ["string", "null"],
            "description": "The one question you would ask the user. Null when action is "
            "'sql'.",
        },
        "interpretation": {
            "type": "string",
            "description": (
                "One or two plain-English sentences saying what the query actually "
                "computes, written for someone who cannot read SQL, so they can catch a "
                "misunderstanding. Name the filters and the grouping explicitly."
            ),
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any assumption you had to make. Empty if none.",
        },
    },
    "required": ["action", "interpretation", "assumptions"],
    "additionalProperties": False,
}

BASE_ROLE = (
    "You translate questions about a learning platform into PostgreSQL queries.\n"
    "You are precise about the difference between what was asked and what the data can "
    "answer."
)

SQL_RULES = """\
Rules for the SQL you produce:
1. One statement. A SELECT (optionally with CTEs). Never INSERT, UPDATE, DELETE, DROP,
   ALTER, GRANT or a data-modifying CTE.
2. Always include a LIMIT. Use LIMIT 100 unless the question implies a specific count.
3. Use only tables and columns from the schema above. Never invent a column.
4. Use the exact enum spellings shown. 'active', not 'ACTIVE' or 'Active'.
5. Qualify every column with a table alias when more than one table is involved.
6. Prefer explicit JOIN ... ON over comma joins.
7. Filter the large `submissions` table before aggregating it wherever the question
   allows it.
8. Money columns are integer cents: divide by 100.0 to report dollars.
9. When a question asks for a rate or percentage, multiply by 100.0 (not 100) so the
   division is not integer division.

When to ask instead of answering:
Set action = "clarify" only when the question has two or more defensible readings that
would produce materially different numbers, and you cannot choose between them from the
schema. Examples of genuine ambiguity: an undefined term ("best", "active", "engaged"),
an unstated time window where the answer depends on it, or an unspecified entity when
several match. Do NOT ask for clarification merely because a question is broad or because
you must pick a sensible default like a row limit or an ordering.
"""

COT_RULES = """\
Before writing the SQL, work out in this order:
  a. Which tables hold the facts being asked about.
  b. The join path between them, following the foreign keys.
  c. The filters, including any status filter the domain notes require.
  d. The grain of the answer: one row per what?
Then write the query. Put the conclusion of that reasoning in `interpretation`.
"""


# ---------------------------------------------------------------------------
# e-commerce domain
# ---------------------------------------------------------------------------

ECOMMERCE_ROLE = (
    "You translate questions about an online store (customers, products, orders, "
    "payments, reviews) into PostgreSQL queries.\n"
    "You are precise about the difference between what was asked and what the data can "
    "answer."
)

ECOMMERCE_RULES = """\
Rules for the SQL you produce:
1. One statement. A SELECT (optionally with CTEs). Never INSERT, UPDATE, DELETE, DROP,
   ALTER, GRANT or a data-modifying CTE. Never query pg_catalog / information_schema.
2. Always include a LIMIT unless the query returns a single aggregate row. Use LIMIT 100
   unless the question implies a specific count.
3. Use only tables and columns from the schema above. Never invent a column.
4. Use the exact enum spellings shown. 'delivered', not 'Delivered'.
5. Qualify every column with a table alias when more than one table is involved.
6. Always JOIN ... ON a foreign key. Never CROSS JOIN two tables and never write a comma
   join without a join condition.
7. Money columns (price, total_amount, amount, unit_price) are dollars. Line revenue is
   quantity * unit_price * (1 - discount_pct).
8. When a question asks for a rate or percentage, multiply by 100.0 (not 100) so the
   division is not integer division.
9. NULL is not a value: use IS NULL / IS NOT NULL, and COALESCE when summing nullable
   columns. A LEFT JOIN is needed to keep rows that have no match.
10. The data ends on 2025-01-01. Never use now() or current_date; express dates literally.

When to ask instead of answering:
Set action = "clarify" only when the question has two or more defensible readings that
would produce materially different numbers, or cannot be answered from this schema at
all. Do NOT ask for clarification merely because a question is broad or because you must
pick a sensible default like a row limit or an ordering.
"""

ECOMMERCE_NOTES = """\
Domain notes:
- order_items is the largest table (25,000 rows); filter on an indexed column
  (order_id, product_id) or aggregate it rather than filtering on discount_pct/quantity.
- orders.status lifecycle: placed -> paid -> shipped -> delivered; or cancelled / refunded.
- payments has one row per order; money moved only when payments.status = 'succeeded'.
- shipped_at / delivered_at are NULL until the event happens; reviews.body and
  orders.coupon_code are NULL when absent; categories.parent_id is NULL for top-level
  categories."""

DOMAINS: dict[str, tuple[str, str, str | None]] = {
    # name -> (role, rules, schema notes; None = serializer default)
    "learning": (BASE_ROLE, SQL_RULES, None),
    "ecommerce": (ECOMMERCE_ROLE, ECOMMERCE_RULES, ECOMMERCE_NOTES),
}


@dataclass(frozen=True)
class BuiltPrompt:
    """A prompt ready to send, with the pieces kept separate for caching and for debugging."""

    system: str
    user: str
    examples: tuple[FewShotExample, ...]
    schema_format: SchemaFormat
    strategy: PromptStrategy

    def approx_tokens(self) -> int:
        """Rough size estimate for the cost table. Four characters per token is a decent
        rule of thumb for English + SQL; the real number comes from count_tokens()."""
        return (len(self.system) + len(self.user)) // 4


def render_examples(examples: list[FewShotExample]) -> str:
    blocks = []
    for ex in examples:
        note = f"\n-- {ex.note}" if ex.note else ""
        blocks.append(f"Question: {ex.question}{note}\nSQL:\n{ex.sql}")
    return "\n\n".join(blocks)


def build_prompt(
    question: str,
    catalog: Catalog,
    *,
    strategy: PromptStrategy = "structured",
    schema_format: SchemaFormat = "ddl",
    few_shot_count: int = 4,
    error_feedback: str | None = None,
    previous_sql: str | None = None,
    domain: str = "learning",
) -> BuiltPrompt:
    """Assemble the system and user messages.

    The system message holds everything stable across requests -- role, schema, rules,
    examples -- so it can be marked for prompt caching. The user message holds only the
    question and any retry feedback. Putting the question in the system message would
    invalidate the cache on every request, the most common prompt-caching mistake.
    """
    role, rules, notes = DOMAINS[domain]
    schema_text = serialize(catalog, schema_format) if notes is None else serialize(
        catalog, schema_format, notes=notes
    )

    if strategy == "minimal":
        system = (
            f"{role}\n\nSchema:\n{schema_text}\n\n"
            "Return one PostgreSQL SELECT with a LIMIT."
        )
        examples: list[FewShotExample] = []
    else:
        # The few-shot bank holds learning-platform queries only.
        examples = select_examples(question, few_shot_count) if domain == "learning" else []
        parts = [role, "", "Schema:", schema_text, "", rules]
        if strategy == "cot":
            parts += ["", COT_RULES]
        if examples:
            parts += ["", "Worked examples for this database:", "", render_examples(examples)]
        system = "\n".join(parts)

    user_parts = [f"Question: {question}"]
    if error_feedback:
        # The retry turn. The previous attempt and the exact machine-generated reason it
        # was rejected are both included: "it was invalid" is not actionable, whereas
        # "[UNKNOWN_COLUMN] Column 'submitted_date' does not exist" is.
        user_parts += [
            "",
            "Your previous attempt was rejected before it ran.",
            "Previous SQL:",
            previous_sql or "(none)",
            "",
            "Rejection reasons:",
            error_feedback,
            "",
            "Fix exactly these problems and return the corrected query.",
        ]
    return BuiltPrompt(
        system=system,
        user="\n".join(user_parts),
        examples=tuple(examples),
        schema_format=schema_format,
        strategy=strategy,
    )
