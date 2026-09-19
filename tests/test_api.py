"""HTTP surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nl2sql.api import app
from tests.conftest import needs_db


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@needs_db
def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["statement_timeout_ms"] > 0
    assert body["tables"]["submissions"] > 1_000_000


@needs_db
def test_schema_endpoint(client: TestClient) -> None:
    body = client.get("/api/schema?fmt=compact").json()
    assert len(body["tables"]) == 8
    assert "enrollments" in body["text"]


@needs_db
def test_schema_endpoint_rejects_unknown_format(client: TestClient) -> None:
    assert client.get("/api/schema?fmt=yaml").status_code == 400


@needs_db
def test_index_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "nl2sql" in response.text


@needs_db
def test_ask_returns_the_four_things_a_user_needs(client: TestClient) -> None:
    """SQL, plain English, the plan, and the rows -- a bare number is unfalsifiable."""
    body = client.post(
        "/api/ask", json={"question": "Which courses have the most enrollments?"}
    ).json()
    assert body["status"] in {"ok", "clarify", "rejected", "error"}
    for field in ("sql", "interpretation", "plan_summary", "rows", "retries"):
        assert field in body


@needs_db
def test_ask_rejects_an_empty_question(client: TestClient) -> None:
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


@needs_db
def test_rows_are_json_safe(client: TestClient) -> None:
    """Decimal and datetime cannot cross JSON; they are stringified once, server-side."""
    body = client.post(
        "/api/ask", json={"question": "Which courses have the most enrollments?"}
    ).json()
    for row in body["rows"]:
        for value in row:
            assert value is None or isinstance(value, (str, int, float, bool))
