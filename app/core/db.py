"""Postgres access layer (Supabase) — connections, tenant-scoped RLS sessions, logging.

Design:
  - One Supabase Postgres holds BOTH the pgvector `chunks` table and the relational
    tables (`tenants`, `conversations`, `leads`).
  - Row-Level Security isolates tenants. Every read/write of tenant data runs inside a
    session that has set the GUC `app.tenant_id`. RLS policies compare rows against
    `current_setting('app.tenant_id', true)`, so a session scoped to tenant A physically
    cannot read tenant B's rows. We ALSO filter by tenant_id in SQL (defense in depth).
  - Connections are opened per operation (simple + correct for the demo scale). Swap in a
    pool later if needed.

NOTE: raw PII is NEVER written here except the opt-in `leads.contact` column, which is the
single intentional PII store (see capture_lead / supabase_setup.sql).
"""
from __future__ import annotations

import contextlib
import json
from typing import Iterator, Optional

from .config import settings

# psycopg (v3) + pgvector are optional at import time so offline unit tests (no DB, no keys)
# can import the package without these installed / without a live DB.
try:
    import psycopg
    from pgvector.psycopg import register_vector
    _HAVE_PG = True
except Exception:  # pragma: no cover - only when deps/DB absent
    psycopg = None  # type: ignore
    register_vector = None  # type: ignore
    _HAVE_PG = False


def available() -> bool:
    """True when psycopg is importable AND a DATABASE_URL is configured."""
    return _HAVE_PG and bool(settings.DATABASE_URL)


@contextlib.contextmanager
def connect(dsn: Optional[str] = None, readonly: bool = False) -> Iterator["psycopg.Connection"]:
    """Open a short-lived connection. Registers the pgvector type adapter."""
    if not _HAVE_PG:
        raise RuntimeError("psycopg/pgvector not installed")
    url = dsn or (settings.READONLY_DATABASE_URL if readonly else settings.DATABASE_URL)
    if not url:
        raise RuntimeError("DATABASE_URL not configured")
    conn = psycopg.connect(url)
    try:
        try:
            register_vector(conn)
        except Exception:
            # register_vector needs the `vector` extension present; ignore for non-vector conns.
            pass
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def tenant_session(tenant: str, readonly: bool = False) -> Iterator["psycopg.Connection"]:
    """A connection whose session is scoped to `tenant` for RLS.

    Sets `app.tenant_id` as a session GUC (is_local=false so it persists for the connection).
    All RLS policies key off current_setting('app.tenant_id', true).
    """
    with connect(readonly=readonly) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Bootstrap: seed tenants from the TENANT_KEYS env into the DB (hashed keys).
# --------------------------------------------------------------------------- #
def bootstrap_seed_tenants() -> int:
    """UPSERT seed tenants from TENANT_KEYS. Keys stored as sha256 hashes, never plaintext.

    Returns the number of seed tenants processed. No-op (returns 0) if the DB is unavailable.
    """
    from .security import hash_key  # local import avoids a cycle

    if not available():
        return 0
    seeds = settings.tenant_map()
    if not seeds:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            for tid, key in seeds.items():
                cur.execute(
                    """
                    INSERT INTO tenants (id, name, api_key_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                      SET api_key_hash = EXCLUDED.api_key_hash
                    """,
                    (tid, tid, hash_key(key)),
                )
        conn.commit()
    return len(seeds)


def resolve_tenant_by_hash(api_key_hash: str) -> Optional[str]:
    """Return the tenant id whose api_key_hash matches, else None. DB-backed auth."""
    if not available():
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tenants WHERE api_key_hash = %s LIMIT 1", (api_key_hash,))
            row = cur.fetchone()
    return row[0] if row else None


def resolve_widget_token(public_token: str) -> Optional[dict]:
    """Resolve a PUBLIC widget token (wt_...) to its agent + tenant, or None if not found.

    Joins widget_tokens -> agents. `tenant` comes from `agents.config->>'tenant'`
    (defaults to 'demo' when unset). `allowed_origins` is the token's CORS allow-list.
    The widget token is PUBLIC by design (embedded on client sites), so this returns no
    secret. Values are JSON-safe (uuid -> str).

    Returns {agent_id, tenant, allowed_origins, wt_status, agent_status} or None.
    """
    if not available() or not public_token:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT wt.agent_id, wt.allowed_origins, wt.status AS wt_status,
                       a.status AS agent_status, COALESCE(a.config->>'tenant', 'demo') AS tenant
                FROM widget_tokens wt
                JOIN agents a ON a.id = wt.agent_id
                WHERE wt.public_token = %s
                LIMIT 1
                """,
                (public_token,),
            )
            row = cur.fetchone()
    if not row:
        return None
    agent_id, allowed_origins, wt_status, agent_status, tenant = row
    return {
        "agent_id": str(agent_id),
        "tenant": tenant or "demo",
        "allowed_origins": list(allowed_origins or []),
        "wt_status": wt_status,
        "agent_status": agent_status,
    }


# --------------------------------------------------------------------------- #
# Conversation + lead logging (tenant-scoped, RLS-enforced).
# --------------------------------------------------------------------------- #
def log_conversation(
    tenant: str,
    question_redacted: str,
    answer: str,
    grounded: bool,
    sources: list[str],
    pii_types: list[str],
    injection_flag: bool,
) -> None:
    """Persist one /chat turn. question_redacted MUST already be PII-free."""
    if not available():
        return
    with tenant_session(tenant) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversations
                  (tenant_id, question_redacted, answer, grounded, sources, pii_types, injection_flag)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (tenant, question_redacted, answer, grounded,
                 json.dumps(sources), json.dumps(pii_types), injection_flag),
            )
        conn.commit()


def capture_lead(tenant: str, question_redacted: str, contact: str) -> None:
    """Opt-in lead capture. `contact` is the ONE intentional PII store (sensitive; encrypt at rest)."""
    if not available():
        raise RuntimeError("DATABASE_URL not configured")
    with tenant_session(tenant) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (tenant_id, question_redacted, contact) VALUES (%s, %s, %s)",
                (tenant, question_redacted, contact),
            )
        conn.commit()
