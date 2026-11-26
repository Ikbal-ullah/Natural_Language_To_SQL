"""Schema linking: choose which tables the model gets to see.

Sending the whole schema works for 7 tables and stops working at 70: prompt cost grows
linearly, and a model shown forty irrelevant tables joins them. The linker narrows the
prompt to the tables a question is plausibly about.

ALGORITHM (deterministic, no model call, no embeddings)
  1. Each table becomes a small document: its name (weighted x3), comment, column names,
     column comments, enum values and sample values.
  2. BM25 (k1=1.5, b=0.75) scores every table document against the question tokens.
  3. SEEDS = the top-k tables with a positive score.
  4. 1-HOP FK NEIGHBOURS: tables that a seed references or that reference a seed are
     appended (best BM25 score first) until `max_tables`. Join paths between seeds usually
     run through such a bridge table, and without it the question is unanswerable.
  5. If nothing scores above zero the linker returns the FULL schema. Failing open to "show
     everything" costs tokens; failing closed to "show nothing" costs the answer.

The linker narrows only the PROMPT. Security validation always runs against the full
catalog, so a linking mistake can make an answer wrong but can never widen what the
generated SQL is allowed to touch. The pipeline widens to the full schema when a retry is
triggered by an unknown table/column, so a recall miss is recoverable inside the retry budget.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from nl2sql.examples import tokenize
from nl2sql.schema import Catalog, Table

K1, B = 1.5, 0.75
NAME_WEIGHT = 3


def _tokens(text: str) -> list[str]:
    return tokenize(re.sub(r"_", " ", text).replace("-", " "))


def table_document(table: Table) -> list[str]:
    toks: list[str] = _tokens(table.name) * NAME_WEIGHT
    toks += _tokens(table.comment or "")
    for c in table.columns:
        toks += _tokens(c.name)
        toks += _tokens(c.comment or "")
        for v in (*c.enum_values, *c.samples):
            toks += _tokens(v)
    return toks


@dataclass
class LinkResult:
    catalog: Catalog                      # the narrowed catalog to show the model
    selected: list[str]                   # seeds first, then neighbours
    seeds: list[str]
    scores: dict[str, float] = field(default_factory=dict)
    fell_back: bool = False               # True when nothing matched and we returned all


def bm25_scores(question: str, catalog: Catalog) -> dict[str, float]:
    docs = {name: table_document(t) for name, t in catalog.tables.items()}
    n = len(docs)
    avgdl = sum(len(d) for d in docs.values()) / max(n, 1)
    df: Counter[str] = Counter()
    for d in docs.values():
        df.update(set(d))
    q = set(_tokens(question))
    scores: dict[str, float] = {}
    for name, d in docs.items():
        tf = Counter(d)
        score = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            score += idf * freq * (K1 + 1) / (freq + K1 * (1 - B + B * len(d) / avgdl))
        scores[name] = score
    return scores


def _neighbours(catalog: Catalog, name: str) -> set[str]:
    out = {fk.ref_table for fk in catalog.tables[name].foreign_keys}
    out |= {
        t.name for t in catalog.tables.values()
        if any(fk.ref_table == name for fk in t.foreign_keys)
    }
    out.discard(name)
    return {t for t in out if t in catalog.tables}


def _restrict(catalog: Catalog, names: list[str]) -> Catalog:
    keep = set(names)
    tables = {
        n: replace(
            catalog.tables[n],
            foreign_keys=tuple(fk for fk in catalog.tables[n].foreign_keys if fk.ref_table in keep),
        )
        for n in names
    }
    return Catalog(tables=tables)


def link_schema(
    question: str, catalog: Catalog, *, top_k: int = 2, max_tables: int = 5
) -> LinkResult:
    scores = bm25_scores(question, catalog)
    ranked = sorted((n for n in scores if scores[n] > 0), key=lambda n: (-scores[n], n))
    if not ranked:
        everything = sorted(catalog.tables)
        return LinkResult(catalog, everything, [], scores, fell_back=True)

    seeds = ranked[:top_k]
    selected = list(seeds)
    neighbours: set[str] = set()
    for s in seeds:
        neighbours |= _neighbours(catalog, s)
    for n in sorted(neighbours - set(seeds), key=lambda n: (-scores[n], n)):
        if len(selected) >= max(max_tables, len(seeds)):
            break
        selected.append(n)
    return LinkResult(_restrict(catalog, selected), selected, seeds, scores)
