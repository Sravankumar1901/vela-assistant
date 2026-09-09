"""Offline privacy tests for the Chat-to-SQL path (no DB / keys / network).

Closes the audit finding "Chat-to-SQL exposes a customer email column to anonymous visitors
and persists the returned rows in conversations.answer without redaction":

  1. the LLM never sees person-identifying columns (SCHEMA_HINT),
  2. SCHEMA_HINT and sql_guard stay in lockstep (every hinted column is allow-listed, none denied),
  3. a SQL answer that somehow carries an email is redacted BEFORE it is returned/streamed,
  4. conversations.answer receives a row_count/safe_sql summary — never the rows —
     on every SQL-routed path (/chat + /widget/chat via run_chat, /chat/stream, /ask-data).

chatsql.run is stubbed to return rows containing an email address, simulating a future
schema/allow-list slip, so the redaction layer is exercised independently of the guard.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import chatsql, sql_guard

_EMAIL = "maria.silva@example.com"
_FAKE_DATA = {
    "generated_sql": "SELECT customer_id, email FROM customers LIMIT 3",
    "safe_sql": "SELECT customer_id, email FROM customers LIMIT 3",
    "executed": True,
    "blocked_reason": None,
    "columns": ["customer_id", "email"],
    "rows": [[1, _EMAIL], [2, "joao.pereira@example.org"]],
    "row_count": 2,
}
_DATA_QUESTION = "list all customers"  # matches main._DATA_HINT -> route == "sql"


# ---- 1 + 2: schema hint ----------------------------------------------------------

def test_schema_hint_has_no_person_identifying_columns():
    hint = chatsql.SCHEMA_HINT.lower()
    for col in ("first_name", "last_name", "email", "phone", "address"):
        assert col not in hint, f"{col} must not be handed to the LLM"
    # The table itself is still queryable, pseudonymously.
    assert "customers(customer_id, country)" in hint


def test_schema_hint_and_sql_guard_are_in_lockstep():
    tables = dict(re.findall(r"^\s*(\w+)\(([^)]*)\)", chatsql.SCHEMA_HINT, flags=re.MULTILINE))
    assert tables, "could not parse any table(...) lines from SCHEMA_HINT"
    for table, cols in tables.items():
        assert table in sql_guard.ALLOWED_TABLES, table
        for col in (c.strip() for c in cols.split(",") if c.strip()):
            assert col in sql_guard.ALLOWED_COLUMNS, f"{table}.{col} hinted but not allow-listed"
            assert col not in sql_guard.DENIED_COLUMNS, f"{table}.{col} hinted but PII-denied"


# ---- 3: formatted answer is redacted ------------------------------------------------

def test_format_sql_answer_redacts_email():
    out = main._format_sql_answer(_FAKE_DATA)
    assert _EMAIL not in out
    assert "joao.pereira@example.org" not in out
    assert "customer_id" in out  # still a usable answer


def test_sql_summary_never_contains_rows():
    summary = main._sql_summary(_FAKE_DATA)
    assert _EMAIL not in summary
    assert "row_count=2" in summary and "safe_sql=" in summary
    blocked = main._sql_summary({"executed": False, "blocked_reason": "column not allowed"})
    assert "not executed" in blocked and "column not allowed" in blocked


# ---- 4: every SQL-routed path stores a summary, not rows --------------------------------

@pytest.fixture
def sql_path(monkeypatch):
    """Stub the SQL executor + capture what would be written to conversations.answer."""
    stored = []
    monkeypatch.setattr(main.chatsql, "run", lambda tenant, q: dict(_FAKE_DATA))
    monkeypatch.setattr(main.db, "log_conversation",
                        lambda tenant, q, answer, *a, **k: stored.append(answer))
    monkeypatch.setattr(main.security, "audit", lambda event: None)  # no audit file in tests
    monkeypatch.setattr(main.security, "resolve_tenant", lambda key: "demo" if key else None)
    return stored


def _assert_stored_is_summary(stored):
    assert len(stored) == 1, "exactly one conversation row expected"
    assert _EMAIL not in stored[0]
    assert "Here's what I found" not in stored[0]  # no formatted rows either
    assert "row_count=2" in stored[0]


def test_run_chat_sql_route_redacts_answer_and_stores_summary(sql_path):
    res = main.run_chat("demo", _DATA_QUESTION)
    assert res["route"] == "sql" and res["grounded"] is True
    assert _EMAIL not in res["answer"]
    _assert_stored_is_summary(sql_path)


def test_chat_stream_sql_route_redacts_before_streaming_and_stores_summary(sql_path):
    client = TestClient(main.app)
    r = client.post("/chat/stream", json={"message": _DATA_QUESTION}, headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.text
    assert "event: token" in body and "event: result" in body and "event: done" in body
    assert _EMAIL not in body  # nothing raw reached the wire
    assert '"route": "sql"' in body
    _assert_stored_is_summary(sql_path)


def test_ask_data_redacts_answer_and_stores_summary(sql_path):
    client = TestClient(main.app)
    r = client.post("/ask-data", json={"message": _DATA_QUESTION}, headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert _EMAIL not in r.json()["answer"]
    _assert_stored_is_summary(sql_path)


def test_rag_route_storage_unchanged(sql_path, monkeypatch):
    """Non-SQL turns still persist the answer itself (widget behaviour otherwise unchanged)."""
    monkeypatch.setattr(main.rag, "retrieve", lambda tenant, q: [])
    res = main.run_chat("demo", "do you take walk ins?")
    assert res["route"] == "rag"
    assert sql_path == [res["answer"]]
