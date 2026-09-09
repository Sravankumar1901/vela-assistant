"""Secured Chat-to-SQL: NL -> SQL (LLM) -> safety gate -> read-only execution -> audited.

The generated SQL passes through sql_guard.validate_and_fix() BEFORE it can run, and runs
on a read-only DB role (READONLY_DATABASE_URL) with a statement timeout + row cap. Even if
the LLM emits a DROP or a stacked ';DELETE', the gate blocks it before execution and the
read-only role would refuse a write anyway.
"""
from __future__ import annotations

from . import db, llm, sql_guard
from .config import settings

# Schema description handed to the LLM (Chinook subset, snake_case to match supabase_setup.sql).
#
# PRIVACY: person-identifying columns (customers.first_name / last_name / email) are
# deliberately NOT listed here, so the LLM never sees or selects them. The Chat-to-SQL path is
# reachable by anonymous widget visitors, so it must only ever surface aggregate / pseudonymous
# data. sql_guard.DENIED_COLUMNS is the matching hard block in case the LLM guesses a column.
# customer_id is a surrogate key (fine for "revenue per customer"); country is coarse.
SCHEMA_HINT = """
You write a SINGLE PostgreSQL SELECT statement over this schema (snake_case). No comments, no ';'.
Tables & columns:
  artists(artist_id, name)
  albums(album_id, title, artist_id)
  tracks(track_id, name, album_id, genre_id, unit_price, milliseconds)
  genres(genre_id, name)
  customers(customer_id, country)
  invoices(invoice_id, customer_id, invoice_date, total, billing_country)
  invoice_items(invoice_line_id, invoice_id, track_id, unit_price, quantity)
Rules: SELECT only. One statement. Always include a LIMIT. Name every column explicitly — never
use SELECT * and never reference a column that is not listed above. Refer to customers only by
customer_id or country. Return ONLY the SQL, nothing else.
""".strip()


def generate_sql(question_redacted: str) -> str:
    """Ask the LLM for a SELECT. question_redacted is already PII-free."""
    system = (
        "You are a careful analytics engineer. Translate the user's question into ONE read-only "
        "PostgreSQL SELECT statement. Output ONLY raw SQL — no prose, no markdown fences, no semicolon."
    )
    user = f"{SCHEMA_HINT}\n\nQUESTION: {question_redacted}\n\nSQL:"
    raw = llm.raw_complete(system, user, temperature=0.0)
    return _strip_fences(raw)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        # drop an optional leading language tag like "sql\n"
        if "\n" in t:
            first, rest = t.split("\n", 1)
            if first.strip().lower() in ("sql", "postgres", "postgresql"):
                t = rest
    return t.strip()


def run(tenant: str, question_redacted: str) -> dict:
    """Full pipeline. Returns an audit-friendly dict (never contains raw PII)."""
    result = {
        "generated_sql": None,
        "safe_sql": None,
        "executed": False,
        "blocked_reason": None,
        "columns": [],
        "rows": [],
        "row_count": 0,
    }

    generated = generate_sql(question_redacted)
    result["generated_sql"] = generated

    # HARD GATE — before any execution.
    try:
        safe_sql = sql_guard.validate_and_fix(generated)
    except sql_guard.SqlRejected as e:
        result["blocked_reason"] = str(e)
        return result
    result["safe_sql"] = safe_sql

    # H3: never execute Chat-to-SQL on the write-capable main role. Require a genuinely
    # read-only role; if none is configured, the feature is disabled (fails closed).
    if not settings.READONLY_DATABASE_URL:
        result["blocked_reason"] = "Chat-to-SQL disabled: no read-only DB role configured"
        return result

    if not db.available():
        result["blocked_reason"] = "database not configured"
        return result

    # Execute on the READ-ONLY role, with a statement timeout and row cap.
    with db.connect(readonly=True) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Mark the transaction read only FIRST (must precede any query), so any
            # write fails here too. Both are SET commands, not queries, so ordering holds.
            cur.execute("SET TRANSACTION READ ONLY")
            # statement_timeout can't be a bound param; inline as a validated int (config, not user input).
            cur.execute(f"SET LOCAL statement_timeout = {int(settings.SQL_TIMEOUT_MS)}")
            cur.execute(safe_sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchmany(settings.SQL_ROW_CAP)
        conn.rollback()  # no side effects, ever

    result["executed"] = True
    result["columns"] = cols
    result["rows"] = [list(r) for r in rows]
    result["row_count"] = len(rows)
    return result
