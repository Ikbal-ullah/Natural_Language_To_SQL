"""Execution-accuracy comparison semantics."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from eval.comparator import compare, normalize_value


def test_numeric_types_agree() -> None:
    assert normalize_value(Decimal("3.14")) == normalize_value(3.14)
    assert normalize_value(7) == normalize_value(7.0)


def test_bool_is_not_an_int() -> None:
    assert normalize_value(True) is True


def test_dates_normalise_to_iso() -> None:
    assert normalize_value(dt.date(2024, 3, 1)) == "2024-03-01"


def test_row_order_only_matters_when_asked() -> None:
    a, b = [(1,), (2,)], [(2,), (1,)]
    assert compare(a, b).match
    assert not compare(a, b, order_matters=True).match


def test_duplicates_are_significant() -> None:
    """A multiset, not a set: a query that lost a duplicate row is a different answer."""
    assert not compare([(1,), (1,), (2,)], [(1,), (2,), (2,)]).match
    assert not compare([(1,), (1,)], [(1,)]).match


def test_column_permutation_is_tolerated() -> None:
    result = compare([("a", 1)], [(1, "a")])
    assert result.match and result.permuted


def test_shape_mismatch_is_reported_clearly() -> None:
    assert "row count" in compare([(1,), (2,)], [(1,)]).reason
    assert "column count" in compare([(1, 2)], [(1,)]).reason


def test_float_tolerance() -> None:
    assert compare([(0.1 + 0.2,)], [(0.3,)]).match
    assert not compare([(1.0,)], [(1.05,)]).match
