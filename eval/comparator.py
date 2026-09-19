"""Execution accuracy: does the generated query return the same rows as the reference?

WHY NOT COMPARE THE SQL TEXT
----------------------------
String comparison -- exact match, edit distance, BLEU -- is the wrong metric here, and
not by a small margin. It is wrong in both directions at once:

  FALSE NEGATIVES (correct SQL scored wrong). These four queries are identical in every
  respect that matters and share almost no characters:

      SELECT count(*) FROM learners WHERE country_code = 'DE'
      SELECT count(1) FROM learners WHERE 'DE' = country_code
      SELECT count(*) AS n FROM learners l WHERE l.country_code = 'DE'
      WITH d AS (SELECT * FROM learners WHERE country_code = 'DE') SELECT count(*) FROM d

    Alias choice, join order, JOIN vs WHERE, CTE vs subquery, EXISTS vs IN, and whitespace
    are all free variables. A metric that punishes them measures conformity to one
    author's style, and the number it produces goes down when the model writes better SQL
    in a different idiom.

  FALSE POSITIVES (wrong SQL scored right), which are worse. Change `>=` to `>`, or drop
  `DISTINCT`, or use an INNER JOIN where a LEFT JOIN was needed, and you have a query that
  is ~99% similar as text and returns a materially different answer. A metric that scores
  that as 0.99 is not merely imprecise, it is pointed the wrong way: it rewards the class
  of error you most need to catch.

Execution accuracy avoids both because it compares what the user actually receives. Its
own weaknesses are real and worth naming rather than hiding:

  * It needs a populated database, so it cannot be run on schema alone.
  * It can be fooled by coincidence on a small or degenerate dataset -- two different
    queries returning the same rows by luck. This is exactly why the seed is 5,000,000
    skewed rows and why a reference query returning zero rows is treated as a bug in the
    evaluation set (see sq08): on an empty result, every wrong query is also 'correct'.
  * It cannot distinguish a query that is right from one that is right today. A missing
    time filter can agree with the reference until the data moves.

We accept those and note them, because the alternative measures the wrong thing entirely.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from itertools import permutations
from typing import Any

# Numeric answers must not fail on representation. 0.1+0.2 is not 0.3 in floating point,
# round(avg(x),2) and avg(x) rounded in Python differ in the last digit, and NUMERIC and
# float8 disagree harmlessly. Six decimal places is far tighter than any question here
# cares about and far looser than the noise.
FLOAT_TOLERANCE = 1e-6
# Trying every column permutation is factorial. Eight columns is 40,320 permutations,
# which is fine; beyond that we stop permuting and compare positionally, because a
# question with more than eight output columns is not being saved by a reordering.
MAX_PERMUTE_COLUMNS = 8


@dataclass(frozen=True)
class ComparisonResult:
    match: bool
    reason: str
    reference_rows: int = 0
    candidate_rows: int = 0
    permuted: bool = False


def normalize_value(value: Any) -> Any:
    """Map a database value onto something comparable across two correct queries.

    The judgement calls, each of which is a decision about what 'the same answer' means:
      * Decimal and float are the same number. PostgreSQL returns NUMERIC for
        round(avg(...)) and float8 for avg(...), and a metric that calls those different
        is measuring types, not answers.
      * Whole-number floats compare equal to ints: count(*) is bigint, sum(...)/1 is
        numeric, and 42 is 42.
      * Strings are stripped. Trailing whitespace from a char(2) column is not a
        different answer.
      * datetime and date compare by ISO text, so timezone-aware and naive values that
        denote the same instant do not accidentally differ by object identity.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return round(value, 6)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return tuple(normalize_value(v) for v in value)
    return str(value)


def normalize_rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [tuple(normalize_value(v) for v in row) for row in rows]


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= FLOAT_TOLERANCE
    return a == b


def _rows_equal(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool:
    return len(a) == len(b) and all(_values_equal(x, y) for x, y in zip(a, b, strict=True))


def _multiset_equal(
    left: list[tuple[Any, ...]], right: list[tuple[Any, ...]]
) -> bool:
    """Order-insensitive comparison that still respects duplicates.

    A multiset, not a set: if the reference returns a value twice, a query returning it
    once has not produced the same answer. Counter needs hashable rows, and floats have
    already been rounded to six places by normalize_value, so equal values hash equally.
    """
    if len(left) != len(right):
        return False
    try:
        return Counter(left) == Counter(right)
    except TypeError:  # pragma: no cover - unhashable exotic type
        remaining = list(right)
        for row in left:
            for i, other in enumerate(remaining):
                if _rows_equal(row, other):
                    remaining.pop(i)
                    break
            else:
                return False
        return not remaining


def _ordered_equal(left: list[tuple[Any, ...]], right: list[tuple[Any, ...]]) -> bool:
    return len(left) == len(right) and all(
        _rows_equal(a, b) for a, b in zip(left, right, strict=True)
    )


def compare(
    reference_rows: list[tuple[Any, ...]],
    candidate_rows: list[tuple[Any, ...]],
    *,
    order_matters: bool = False,
    allow_column_permutation: bool = True,
) -> ComparisonResult:
    """Compare two result sets.

    `order_matters` is set per question, not globally. Most questions have no inherent
    order and forcing one would fail correct queries that happen to sort differently; but
    a question that says 'the five most expensive' is asking about order, and a query that
    returns the right five in the wrong sequence has not answered it. Getting this wrong
    in either direction quietly moves the headline number by several points.

    `allow_column_permutation` accepts a query that produces the right columns in a
    different order. That is the convention in the text-to-SQL literature, and the reason
    is that column order is rarely part of the question. It costs something: if two
    columns hold identical values, a query that swapped their meanings still matches. That
    is a real hole and it is the price of not failing correct queries over cosmetics.
    """
    ref = normalize_rows(reference_rows)
    cand = normalize_rows(candidate_rows)

    if len(ref) != len(cand):
        return ComparisonResult(
            False,
            f"row count differs: reference {len(ref)}, candidate {len(cand)}",
            len(ref),
            len(cand),
        )
    if not ref:
        # Both empty. Reported as a match, but the evaluation set is guarded against
        # reference queries that return nothing, because such a question cannot
        # distinguish a correct query from a broken one.
        return ComparisonResult(True, "both result sets are empty", 0, 0)

    ref_width = len(ref[0])
    cand_width = len(cand[0])
    if ref_width != cand_width:
        return ComparisonResult(
            False,
            f"column count differs: reference {ref_width}, candidate {cand_width}",
            len(ref),
            len(cand),
        )

    equal = _ordered_equal if order_matters else _multiset_equal
    if equal(ref, cand):
        return ComparisonResult(True, "exact match", len(ref), len(cand))

    if allow_column_permutation and 1 < ref_width <= MAX_PERMUTE_COLUMNS:
        for order in permutations(range(cand_width)):
            if order == tuple(range(cand_width)):
                continue
            reordered = [tuple(row[i] for i in order) for row in cand]
            if equal(ref, reordered):
                return ComparisonResult(
                    True,
                    f"match after reordering candidate columns to {order}",
                    len(ref),
                    len(cand),
                    permuted=True,
                )

    sample_ref = ref[0] if ref else None
    sample_cand = cand[0] if cand else None
    detail = "row order differs" if _multiset_equal(ref, cand) else "row values differ"
    return ComparisonResult(
        False,
        f"{detail}; first reference row {sample_ref!r}, first candidate row {sample_cand!r}",
        len(ref),
        len(cand),
    )
