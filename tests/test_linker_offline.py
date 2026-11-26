"""Database-free tests: schema linker and scalar-query detection on a synthetic catalog."""

from __future__ import annotations

import sqlglot

from nl2sql.linker import bm25_scores, link_schema
from nl2sql.schema import Catalog, Column, ForeignKey, Table
from nl2sql.validator import is_scalar_query


def _t(name: str, cols: list[str], fks: tuple[ForeignKey, ...] = (), comment: str | None = None) -> Table:
    return Table(name, comment, tuple(Column(c, "text", False) for c in cols), ("id",), fks, (), (), 100)


CAT = Catalog(tables={
    "customers": _t("customers", ["id", "email", "country"]),
    "orders": _t("orders", ["id", "customer_id", "total"], (ForeignKey(("customer_id",), "customers", ("id",)),)),
    "reviews": _t("reviews", ["id", "customer_id", "rating"], (ForeignKey(("customer_id",), "customers", ("id",)),)),
    "invoices": _t("invoices", ["id", "ledger"], comment="accounting ledger"),
})


def test_bm25_ranks_the_named_table_first() -> None:
    scores = bm25_scores("average review rating", CAT)
    assert max(scores, key=scores.get) == "reviews"


def test_linking_adds_one_hop_fk_neighbours_and_skips_unrelated_tables() -> None:
    res = link_schema("how many orders", CAT, top_k=1, max_tables=3)
    assert res.seeds == ["orders"] and "customers" in res.selected and "invoices" not in res.selected
    # narrowed catalog never shows a foreign key that points outside itself
    for t in res.catalog.tables.values():
        assert all(fk.ref_table in res.catalog.tables for fk in t.foreign_keys)


def test_linking_falls_back_to_full_schema_when_nothing_matches() -> None:
    res = link_schema("qwerty zxcvb", CAT)
    assert res.fell_back and set(res.selected) == set(CAT.tables)


def test_scalar_detection() -> None:
    def scalar(sql: str) -> bool:
        return is_scalar_query(sqlglot.parse_one(sql, dialect="postgres"))

    assert scalar("SELECT count(*) FROM orders")
    assert scalar("SELECT max(total), min(total) FROM orders")
    assert scalar("SELECT 1")
    assert not scalar("SELECT id FROM orders")
    assert not scalar("SELECT status, count(*) FROM orders GROUP BY status")
    assert not scalar("SELECT count(*) OVER () FROM orders")
    assert not scalar("SELECT id, count(*) FROM orders")
