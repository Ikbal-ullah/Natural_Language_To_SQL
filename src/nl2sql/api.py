"""FastAPI application.

The response deliberately returns more than the answer. A number on its own is
unfalsifiable: the user cannot tell a correct query from a plausible wrong one, and the
system is at its most dangerous when it is confidently wrong about an ambiguous question.
So every response carries four things, and the front end shows them side by side:

    sql             what was actually run, so someone who reads SQL can check it
    interpretation  what was run, in plain English, so someone who cannot read SQL can
                    still catch "you counted enrollments, I meant learners"
    plan_summary    how the database answered it, so a full scan over 5M rows is visible
                    rather than merely slow
    rows            the answer itself

The interpretation is the one that earns its place most often. Nearly every serious
text-to-SQL failure in practice is not invalid SQL -- that gets caught -- it is valid SQL
answering a subtly different question.
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from nl2sql.config import get_settings
from nl2sql.db import close_pools, table_row_estimates
from nl2sql.pipeline import Answer, Pipeline
from nl2sql.schema import get_catalog
from nl2sql.serialize import serialize
from nl2sql.telemetry import configure_json_logging

STATIC_DIR = pathlib.Path(__file__).parent / "static"

SAMPLE_QUESTIONS = [
    "Which courses have the most enrollments?",
    "What is the completion rate for each course subject?",
    "Show total succeeded revenue by month in 2024.",
    "Which mentors have the highest average session rating?",
    "For each course, who are the top 3 learners by final grade?",
    "How many learners have never submitted anything?",
    "Who are our best learners?",
    "Delete every failed payment.",
]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1_000)
    strategy: str | None = None
    schema_format: str | None = None
    few_shot_count: int | None = Field(default=None, ge=0, le=12)
    execute: bool = True
    schema_linking: bool | None = None


class AskResponse(BaseModel):
    status: str
    question: str
    sql: str | None
    interpretation: str
    assumptions: list[str]
    clarification_question: str | None
    plan_summary: str
    plan_findings: list[str]
    warnings: list[str]
    error: str | None
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    retries: int
    attempts: list[dict[str, Any]]
    elapsed_ms: float
    generation_ms: float
    execution_ms: float
    provider: str
    model: str
    tokens: dict[str, int]
    tables_selected: list[str] = []
    plan_cost: float = 0.0
    security_violation: bool = False


_pipeline: Pipeline | None = None


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Warm the pool and the catalog at startup so the first user request does not pay for
    # connection setup and four catalog queries.
    configure_json_logging()
    get_pipeline()
    yield
    close_pools()


app = FastAPI(
    title="nl2sql",
    version="1.0.0",
    description="Natural language to SQL over a learning platform, with safety guarantees.",
    lifespan=lifespan,
)


def to_response(answer: Answer) -> AskResponse:
    return AskResponse(
        status=answer.status,
        question=answer.question,
        sql=answer.sql,
        interpretation=answer.interpretation,
        assumptions=answer.assumptions,
        clarification_question=answer.clarification_question,
        plan_summary=answer.plan_summary,
        plan_findings=answer.plan_findings,
        warnings=answer.warnings,
        error=answer.error,
        columns=answer.columns,
        # JSON cannot carry Decimal or datetime, and the front end only ever displays
        # these, so they are stringified once here rather than guessed at in JavaScript.
        rows=[[_jsonable(v) for v in row] for row in answer.rows],
        row_count=answer.row_count,
        retries=answer.retries,
        attempts=[
            {"index": a.index, "stage": a.stage, "ok": a.ok, "errors": a.errors}
            for a in answer.attempts
        ],
        elapsed_ms=round(answer.elapsed_ms, 1),
        generation_ms=round(answer.generation_ms, 1),
        execution_ms=round(answer.execution_ms, 2),
        provider=answer.provider,
        model=answer.model,
        tokens={
            "input": answer.input_tokens,
            "output": answer.output_tokens,
            "cache_read": answer.cache_read_tokens,
            "cache_write": answer.cache_write_tokens,
        },
        tables_selected=answer.tables_selected,
        plan_cost=round(answer.plan_cost, 2),
        security_violation=answer.security_violation,
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@app.post("/api/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse | JSONResponse:
    pipeline = get_pipeline()
    answer = pipeline.answer(
        request.question,
        strategy=request.strategy,  # type: ignore[arg-type]
        schema_format=request.schema_format,  # type: ignore[arg-type]
        few_shot_count=request.few_shot_count,
        execute=request.execute,
        schema_linking=request.schema_linking,
    )
    response = to_response(answer)
    if answer.security_violation:
        # A security violation is a refusal, not a malformed request: 403, body carries
        # the validator's reason, and the pipeline has already spent zero retries on it.
        return JSONResponse(status_code=403, content=response.model_dump())
    return response


@app.get("/api/schema")
def schema(fmt: str = "compact") -> dict[str, Any]:
    if fmt not in ("ddl", "compact", "markdown", "json"):
        raise HTTPException(400, f"unknown schema format {fmt!r}")
    catalog = get_catalog()
    return {
        "format": fmt,
        "text": serialize(catalog, fmt),  # type: ignore[arg-type]
        "tables": {
            name: {
                "rows": table.row_estimate,
                "columns": [c.name for c in table.columns],
            }
            for name, table in sorted(catalog.tables.items())
        },
    }


@app.get("/api/samples")
def samples() -> dict[str, list[str]]:
    return {"questions": SAMPLE_QUESTIONS}


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    try:
        rows = table_row_estimates()
        db_ok = True
    except Exception:  # pragma: no cover - only when the database is down
        rows, db_ok = {}, False
    return {
        "ok": db_ok,
        "provider": get_pipeline().provider.name,
        "model": settings.model,
        "statement_timeout_ms": settings.statement_timeout_ms,
        "max_plan_cost": settings.max_plan_cost,
        "tables": rows,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
