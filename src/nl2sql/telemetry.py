"""Structured per-request telemetry: one JSON object per line on logger `nl2sql.telemetry`.

Fields are deliberately limited to operational facts. The question text, the SQL and the
result rows are NOT logged: they can contain customer data, and latency/retry/cost
telemetry does not need them.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nl2sql.pipeline import Answer

LOGGER_NAME = "nl2sql.telemetry"
logger = logging.getLogger(LOGGER_NAME)


def record(answer: Answer, request_id: str | None = None) -> dict[str, object]:
    """Build (and log) the telemetry record for one finished request."""
    rec: dict[str, object] = {
        "event": "nl2sql.request",
        "request_id": request_id or uuid.uuid4().hex[:12],
        "status": answer.status,
        "latency_ms": round(answer.elapsed_ms, 1),
        "retries_used": answer.retries,
        "tables_selected": answer.tables_selected,
        "estimated_cost": round(answer.plan_cost, 2),
        "security_violation": answer.security_violation,
        "schema_linking": answer.schema_linking,
        "generation_ms": round(answer.generation_ms, 1),
        "execution_ms": round(answer.execution_ms, 2),
        "provider": answer.provider,
        "row_count": answer.row_count,
        "question_chars": len(answer.question),
    }
    logger.info(json.dumps(rec, separators=(",", ":")))
    return rec


def configure_json_logging() -> None:
    """Emit bare JSON lines on stdout (call once at API start-up)."""
    if any(getattr(h, "_nl2sql", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._nl2sql = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
