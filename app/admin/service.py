"""Admin portal DB service layer.

All access goes through `db.connect()` (a short-lived psycopg3 connection, tuple/dict
cursors, %s param style). Everything is parameterised — no string-built SQL. Secrets policy:
  - widget_tokens.public_token is PUBLIC by design (embedded on client sites) -> stored + returned in plaintext.
  - api_keys are high-entropy secrets -> ONLY the sha256 hash is stored (via security.hash_key);
    the plaintext key is returned to the caller EXACTLY ONCE (on creation) and never persisted or logged.

Return shapes are fixed so routes + UI can rely on them (see list_clients()).
"""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from ..core import db, rag, security
from ..core.config import settings
from ..core.security import hash_key
from ..ingest import crawl

# psycopg helpers are optional at import time (mirrors core/db.py) so the package
# imports cleanly even without psycopg installed / without a live DB.
try:
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
except Exception:  # pragma: no cover - only when deps absent
    dict_row = None  # type: ignore
    Json = None  # type: ignore


# --- JSON-safe coercion --------------------------------------------------- #
def _s(v):
    """UUID/None -> str/None (ids travel to the UI as strings)."""
    return None if v is None else str(v)


def _iso(v):
    """datetime -> ISO string (None-safe)."""
    return None if v is None else v.isoformat()


def _f(v):
    """Decimal/None -> float (usage totals are numeric in PG)."""
    return 0.0 if v is None else float(v)


def _client_row(r: dict) -> dict:
    return {
        "id": _s(r["id"]),
        "name": r["name"],
        "slug": r["slug"],
        "status": r["status"],
        "brand_accent": r["brand_accent"],
        "website": r.get("website"),
        "contact_name": r.get("contact_name"),
        "contact_email": r.get("contact_email"),
        "contact_phone": r.get("contact_phone"),
        "notes": r.get("notes"),
        "created_at": _iso(r.get("created_at")),
    }


def _agent_row(r: dict) -> dict:
    return {
        "id": _s(r["id"]),
        "client_id": _s(r["client_id"]),
        "type": r["type"],
        "name": r["name"],
        "status": r["status"],
        "plan_id": _s(r.get("plan_id")),
        "plan_name": r.get("plan_name"),
        "llm_model": r.get("llm_model"),
        "config": r.get("config") or {},
        "created_at": _iso(r.get("created_at")),
    }


# --- Reads ---------------------------------------------------------------- #
def list_clients() -> list[dict]:
    """Every client with its agents nested, each agent carrying CURRENT-period usage.

    The current period is the first day of the current month, computed in SQL
    (`date_trunc('month', now())::date`) so no Python clock is needed.
    """
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, name, slug, status, brand_accent, website,
                       contact_name, contact_email, contact_phone, notes, created_at
                FROM clients
                ORDER BY created_at
                """
            )
            clients = [_client_row(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT a.id, a.client_id, a.type, a.name, a.status, a.plan_id,
                       a.llm_model, a.config, a.created_at,
                       p.name AS plan_name,
                       COALESCE(uc.chat_requests, 0)  AS chat_requests,
                       COALESCE(uc.voice_minutes, 0)  AS voice_minutes,
                       COALESCE(uc.cost_usd, 0)       AS cost_usd,
                       EXISTS (
                         SELECT 1 FROM widget_tokens wt
                         WHERE wt.agent_id = a.id AND wt.status = 'active'
                       ) AS has_widget_token,
                       (
                         SELECT count(*) FROM api_keys ak
                         WHERE ak.agent_id = a.id AND ak.status = 'active'
                       ) AS api_key_count
                FROM agents a
                LEFT JOIN plans p ON p.id = a.plan_id
                LEFT JOIN usage_counters uc
                       ON uc.agent_id = a.id
                      AND uc.period = date_trunc('month', now())::date
                ORDER BY a.created_at
                """
            )
            agents_by_client: dict[str, list[dict]] = {}
            for r in cur.fetchall():
                a = _agent_row(r)
                a["usage"] = {
                    "chat_requests": int(r["chat_requests"] or 0),
                    "voice_minutes": _f(r["voice_minutes"]),
                    "cost_usd": _f(r["cost_usd"]),
                }
                a["has_widget_token"] = bool(r["has_widget_token"])
                a["api_key_count"] = int(r["api_key_count"] or 0)
                agents_by_client.setdefault(a["client_id"], []).append(a)

    for c in clients:
        c["agents"] = agents_by_client.get(c["id"], [])
    return clients


def list_plans() -> list[dict]:
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, name, applies_to, price_monthly_usd, incl_chat_requests,
                       incl_voice_minutes, overage_per_request_usd, overage_per_minute_usd,
                       hard_cap, active
                FROM plans
                WHERE active = true
                ORDER BY applies_to, price_monthly_usd
                """
            )
            rows = cur.fetchall()
    return [
        {
            "id": _s(r["id"]),
            "name": r["name"],
            "applies_to": r["applies_to"],
            "price_monthly_usd": _f(r["price_monthly_usd"]),
            "incl_chat_requests": r.get("incl_chat_requests"),
            "incl_voice_minutes": r.get("incl_voice_minutes"),
            "hard_cap": bool(r["hard_cap"]),
        }
        for r in rows
    ]


def get_embed_context(agent_id: str) -> Optional[dict]:
    """Data the route needs to build a chat embed snippet, or None if the agent is missing.

    Returns {type, name, config, brand_accent, public_token} where public_token is the
    agent's ACTIVE widget token (public, safe to embed) or None if there isn't one.
    """
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT a.type, a.name, a.config, c.brand_accent
                FROM agents a
                JOIN clients c ON c.id = a.client_id
                WHERE a.id = %s
                """,
                (agent_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """
                SELECT public_token FROM widget_tokens
                WHERE agent_id = %s AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (agent_id,),
            )
            tok = cur.fetchone()
    return {
        "type": row["type"],
        "name": row["name"],
        "config": row["config"] or {},
        "brand_accent": row["brand_accent"],
        "public_token": tok["public_token"] if tok else None,
    }


# --- Auth (portal login) -------------------------------------------------- #
def verify_admin(email: str, password: str) -> Optional[dict]:
    """Validate a portal login. Looks up admin_users by email and compares
    hash_key(password) (sha256 hex) to the stored password_hash in constant time.

    Returns {email, role} on success, else None. Never returns the hash. Both a
    missing user and a missing/mismatched hash yield None (no oracle on which failed).
    """
    if not email or not password:
        return None
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT email, role, password_hash FROM admin_users WHERE email = %s",
                (email,),
            )
            row = cur.fetchone()
    if not row or not row.get("password_hash"):
        return None
    if not hmac.compare_digest(hash_key(password), row["password_hash"]):
        return None
    return {"email": row["email"], "role": row["role"]}


# --- Dashboard ------------------------------------------------------------ #
def dashboard_metrics() -> dict:
    """Aggregate the numbers the admin dashboard renders. Current period is the
    first day of this month (`date_trunc('month', now())::date`), computed in SQL.

    Shape (all ids->str, datetimes->ISO, Decimals->float):
      mrr, revenue_usd, cost_usd, margin_pct,
      active_clients, total_agents, agents_chat, agents_voice,
      chat_requests, voice_minutes,
      alerts: [{agent, client, pct}]            (>=80% of plan allowance),
      top_clients: [{name, chat_requests, voice_minutes, cost_usd}] (top 5 by cost),
      recent_activity: [{action, target_type, ts}] (last 10),
      usage_trend: [{day, chat_requests, voice_minutes}] (last 14 days).
    """
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # Revenue = MRR of plans on ACTIVE agents.
            cur.execute(
                """
                SELECT COALESCE(SUM(p.price_monthly_usd), 0) AS mrr
                FROM agents a
                JOIN plans p ON p.id = a.plan_id
                WHERE a.status = 'active'
                """
            )
            mrr = _f(cur.fetchone()["mrr"])

            # Cost + usage totals for the current period.
            cur.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0)      AS cost_usd,
                       COALESCE(SUM(chat_requests), 0) AS chat_requests,
                       COALESCE(SUM(voice_minutes), 0) AS voice_minutes
                FROM usage_counters
                WHERE period = date_trunc('month', now())::date
                """
            )
            r = cur.fetchone()
            cost_usd = _f(r["cost_usd"])
            chat_requests = int(r["chat_requests"] or 0)
            voice_minutes = _f(r["voice_minutes"])

            # Client + agent counts.
            cur.execute("SELECT count(*) AS n FROM clients WHERE status = 'active'")
            active_clients = int(cur.fetchone()["n"] or 0)
            cur.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE type = 'chat')  AS chat,
                       count(*) FILTER (WHERE type = 'voice') AS voice
                FROM agents
                """
            )
            ac = cur.fetchone()
            total_agents = int(ac["total"] or 0)
            agents_chat = int(ac["chat"] or 0)
            agents_voice = int(ac["voice"] or 0)

            # Alerts: active agents at >=80% of their plan allowance this period.
            cur.execute(
                """
                SELECT a.name AS agent, c.name AS client,
                       round(
                         CASE
                           WHEN a.type = 'chat'  AND p.incl_chat_requests > 0
                             THEN uc.chat_requests::numeric / p.incl_chat_requests * 100
                           WHEN a.type = 'voice' AND p.incl_voice_minutes > 0
                             THEN uc.voice_minutes / p.incl_voice_minutes * 100
                           ELSE 0
                         END
                       ) AS pct
                FROM agents a
                JOIN clients c ON c.id = a.client_id
                JOIN plans   p ON p.id = a.plan_id
                JOIN usage_counters uc
                       ON uc.agent_id = a.id
                      AND uc.period = date_trunc('month', now())::date
                WHERE a.status = 'active'
                  AND (
                    (a.type = 'chat'  AND p.incl_chat_requests > 0
                       AND uc.chat_requests::numeric / p.incl_chat_requests >= 0.80)
                    OR
                    (a.type = 'voice' AND p.incl_voice_minutes > 0
                       AND uc.voice_minutes / p.incl_voice_minutes >= 0.80)
                  )
                ORDER BY pct DESC
                """
            )
            alerts = [
                {"agent": a["agent"], "client": a["client"], "pct": int(a["pct"] or 0)}
                for a in cur.fetchall()
            ]

            # Top clients by this-period cost.
            cur.execute(
                """
                SELECT c.name AS name,
                       COALESCE(SUM(uc.chat_requests), 0) AS chat_requests,
                       COALESCE(SUM(uc.voice_minutes), 0) AS voice_minutes,
                       COALESCE(SUM(uc.cost_usd), 0)      AS cost_usd
                FROM clients c
                JOIN agents a ON a.client_id = c.id
                JOIN usage_counters uc
                       ON uc.agent_id = a.id
                      AND uc.period = date_trunc('month', now())::date
                GROUP BY c.id, c.name
                ORDER BY cost_usd DESC
                LIMIT 5
                """
            )
            top_clients = [
                {
                    "name": t["name"],
                    "chat_requests": int(t["chat_requests"] or 0),
                    "voice_minutes": _f(t["voice_minutes"]),
                    "cost_usd": _f(t["cost_usd"]),
                }
                for t in cur.fetchall()
            ]

            # Recent activity: last 10 audit entries.
            cur.execute(
                """
                SELECT action, target_type, ts
                FROM admin_audit_log
                ORDER BY ts DESC
                LIMIT 10
                """
            )
            recent_activity = [
                {"action": x["action"], "target_type": x.get("target_type"),
                 "ts": _iso(x["ts"])}
                for x in cur.fetchall()
            ]

            # Usage trend: daily totals for the last 14 days from raw events.
            cur.execute(
                """
                SELECT ts::date AS day,
                       COALESCE(SUM(quantity) FILTER (WHERE kind = 'chat_request'), 0) AS chat_requests,
                       COALESCE(SUM(quantity) FILTER (WHERE kind = 'voice_minute'), 0) AS voice_minutes
                FROM usage_events
                WHERE ts >= now() - interval '14 days'
                GROUP BY ts::date
                ORDER BY day
                """
            )
            usage_trend = [
                {"day": _iso(u["day"]),
                 "chat_requests": _f(u["chat_requests"]),
                 "voice_minutes": _f(u["voice_minutes"])}
                for u in cur.fetchall()
            ]

    revenue_usd = mrr
    margin_pct = round((revenue_usd - cost_usd) / revenue_usd * 100) if revenue_usd else 0
    return {
        "mrr": mrr,
        "revenue_usd": revenue_usd,
        "cost_usd": round(cost_usd, 2),
        "margin_pct": margin_pct,
        "active_clients": active_clients,
        "total_agents": total_agents,
        "agents_chat": agents_chat,
        "agents_voice": agents_voice,
        "chat_requests": chat_requests,
        "voice_minutes": voice_minutes,
        "alerts": alerts,
        "top_clients": top_clients,
        "recent_activity": recent_activity,
        "usage_trend": usage_trend,
    }


# --- Infrastructure ------------------------------------------------------- #
# An honest inventory of what VELA runs on: the Postgres DB (size + per-table),
# the local dev codebase footprint, Cloudflare Pages/Workers (live if a token is
# configured), and a mostly-derived storage inventory. Everything is JSON-safe.

# Repo paths whose on-disk footprint the portal reports. This is a DEV-WORKSPACE
# metric — the backend reads its own local filesystem, not a deployed artifact.
_CODEBASE_PATHS = [
    "/Users/sravan/Documents/partner/vela-assistant",
    "/Users/sravan/Documents/partner/vela-admin",
    "/Users/sravan/Documents/partner/demos/the-coffee-cup",
]
# Dirs never counted (build output, deps, VCS, caches) — keeps the metric meaningful.
_CODEBASE_SKIP_DIRS = {
    "node_modules", ".git", ".next", ".venv", "__pycache__", ".turbo", "dist",
}


def _human_bytes(n: int) -> str:
    """Bytes -> a compact human string (B/KB/MB/GB/TB). Whole bytes, else 1 decimal."""
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"  # pragma: no cover - unreachable, loop covers TB


def _database_infra() -> dict:
    """DB size + a per-table breakdown of PUBLIC-schema user tables (largest first).

    Runs on the MAIN (read/write) connection, not the read-only role — the read-only
    role is intentionally not granted the catalog visibility this needs.
    """
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT pg_database_size(current_database()) AS size_bytes, "
                "pg_size_pretty(pg_database_size(current_database())) AS size_pretty"
            )
            d = cur.fetchone()
            cur.execute(
                """
                select relname as name, n_live_tup as rows,
                  pg_total_relation_size(format('%I.%I', schemaname, relname)::regclass) as size_bytes,
                  pg_size_pretty(pg_total_relation_size(format('%I.%I', schemaname, relname)::regclass)) as size_pretty
                from pg_stat_user_tables where schemaname='public'
                order by size_bytes desc;
                """
            )
            tables = [
                {
                    "name": t["name"],
                    "rows": int(t["rows"] or 0),
                    "size_bytes": int(t["size_bytes"] or 0),
                    "size_pretty": t["size_pretty"],
                }
                for t in cur.fetchall()
            ]
    return {
        "size_bytes": int(d["size_bytes"] or 0),
        "size_pretty": d["size_pretty"],
        "table_count": len(tables),
        "tables": tables,
    }


# Display alias for repo names — COSMETIC (the directory keeps its name on disk;
# the-coffee-cup is the codebase behind the vela-core site).
_REPO_ALIAS = {"the-coffee-cup": "vela-core"}


def _codebase_infra() -> dict:
    """Pure-Python (portable, no shell) footprint of the tracked repos via os.walk.

    Skips dep/build/VCS dirs; ignores files that can't be stat'd. Returns per-repo
    counts + sizes and workspace totals.
    """
    repos = []
    total_files = 0
    total_size = 0
    for path in _CODEBASE_PATHS:
        file_count = 0
        size_bytes = 0
        for root, dirs, files in os.walk(path):
            # Prune skip-dirs in place so os.walk never descends into them.
            dirs[:] = [d for d in dirs if d not in _CODEBASE_SKIP_DIRS]
            for fname in files:
                try:
                    size_bytes += os.path.getsize(os.path.join(root, fname))
                    file_count += 1
                except OSError:
                    continue  # unreadable / broken symlink — skip, don't fail
        _basename = os.path.basename(path.rstrip("/"))
        repos.append({
            "name": _REPO_ALIAS.get(_basename, _basename),
            "path": path,
            "file_count": file_count,
            "size_bytes": size_bytes,
            "size_pretty": _human_bytes(size_bytes),
        })
        total_files += file_count
        total_size += size_bytes
    return {
        "repos": repos,
        "total_files": total_files,
        "total_size_bytes": total_size,
        "total_size_pretty": _human_bytes(total_size),
        "note": "dev-workspace metric (the backend reads the local filesystem)",
    }


def _cloudflare_error(payload, status_code: int) -> str:
    """Best-effort short message from a Cloudflare error payload."""
    try:
        errs = (payload or {}).get("errors") or []
        if errs:
            first = errs[0]
            msg = first.get("message") if isinstance(first, dict) else str(first)
            if msg:
                return str(msg)[:200]
    except Exception:
        pass
    return f"Cloudflare API returned HTTP {status_code}"


# Display aliases for Cloudflare project names — COSMETIC ONLY. Cloudflare Pages names
# are immutable, so we relabel them for the console so they read as ours (the real CF
# project is still "cafe-website-demos").
_PAGES_ALIAS = {"cafe-website-demos": "vela-core"}


def _cloudflare_infra() -> dict:
    """Live Pages/Workers counts when a token + account id are configured, else a
    tidy not-connected shape (never raises)."""
    token = settings.CLOUDFLARE_API_TOKEN
    acct = settings.CLOUDFLARE_ACCOUNT_ID
    if not token or not acct:
        return {
            "connected": False,
            "hint": "Add CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID to enable live "
                    "Pages/Workers counts.",
        }
    try:
        import requests  # available (used by ingest.crawl)

        headers = {"Authorization": f"Bearer {token}"}
        base = f"https://api.cloudflare.com/client/v4/accounts/{acct}"
        pr = requests.get(f"{base}/pages/projects", headers=headers, timeout=15)
        pj = pr.json()
        if not pr.ok or not pj.get("success"):
            return {"connected": False, "error": _cloudflare_error(pj, pr.status_code)}
        wr = requests.get(f"{base}/workers/scripts", headers=headers, timeout=15)
        wj = wr.json()
        if not wr.ok or not wj.get("success"):
            return {"connected": False, "error": _cloudflare_error(wj, wr.status_code)}
        pages = [_PAGES_ALIAS.get(p.get("name"), p.get("name"))
                 for p in (pj.get("result") or []) if p.get("name")]
        workers = [w.get("id") for w in (wj.get("result") or []) if w.get("id")]
        return {
            "connected": True,
            "pages_count": len(pages),
            "workers_count": len(workers),
            "pages": pages,
            "workers": workers,
        }
    except Exception as e:  # network/timeout/JSON — report, never crash the page
        return {"connected": False, "error": str(e)[:200] or "Cloudflare request failed"}


def _supabase_bucket_count() -> Optional[int]:
    """count(*) from Supabase's storage.buckets, or None if the schema/table isn't there.

    Uses its OWN connection so a failing query can't poison an outer transaction.
    """
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from storage.buckets")
                return int(cur.fetchone()[0] or 0)
    except Exception:
        return None


def _storage_infra() -> dict:
    """Honest storage inventory — mostly derived, no external object store is configured."""
    return {
        "object_storage": "None configured (no S3 / R2 bucket)",
        "static_hosting": "Cloudflare Pages",
        "database": "Supabase Postgres",
        "supabase_buckets": _supabase_bucket_count(),
    }


def infrastructure_stats() -> dict:
    """What VELA runs on: database, codebase footprint, Cloudflare, storage inventory.

    All values are JSON-safe (int/float/str). Cloudflare + Supabase-bucket lookups
    degrade gracefully rather than raising, so the page always renders.
    """
    return {
        "database": _database_infra(),
        "codebase": _codebase_infra(),
        "cloudflare": _cloudflare_infra(),
        "storage": _storage_infra(),
    }


# --- Writes --------------------------------------------------------------- #
def create_client(
    name: str,
    slug: str,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    website: Optional[str] = None,
    brand_accent: str = "#c9a25a",
) -> dict:
    """Insert a client. Raises ValueError on a duplicate slug."""
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM clients WHERE slug = %s", (slug,))
            if cur.fetchone():
                raise ValueError(f"slug '{slug}' is already taken")
            cur.execute(
                """
                INSERT INTO clients
                    (name, slug, contact_name, contact_email, contact_phone, website, brand_accent)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, name, slug, status, brand_accent, website,
                          contact_name, contact_email, contact_phone, notes, created_at
                """,
                (name, slug, contact_name, contact_email, contact_phone, website, brand_accent),
            )
            row = cur.fetchone()
        conn.commit()
    return _client_row(row)


def create_agent(
    client_id: str,
    type: str,
    name: str,
    plan_id: Optional[str] = None,
    config: Optional[dict] = None,
) -> dict:
    """Insert an agent (type in ('chat','voice')). Raises ValueError on bad type,
    unknown client, or a duplicate (client, type, name)."""
    if type not in ("chat", "voice"):
        raise ValueError("type must be 'chat' or 'voice'")
    cfg = config if config is not None else {}
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM clients WHERE id = %s", (client_id,))
            if not cur.fetchone():
                raise ValueError("client not found")
            cur.execute("SELECT 1 FROM agents WHERE client_id=%s AND type=%s AND name=%s",
                        (client_id, type, name))
            if cur.fetchone():
                raise ValueError("an agent with this type and name already exists for this client")
            cur.execute(
                """
                INSERT INTO agents (client_id, type, name, plan_id, config)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, client_id, type, name, status, plan_id, llm_model, config, created_at
                """,
                (client_id, type, name, plan_id, Json(cfg)),
            )
            row = cur.fetchone()
        conn.commit()
    out = _agent_row(row)
    out["plan_name"] = None
    return out


def set_agent_status(agent_id: str, status: str) -> None:
    """Set an agent's status. Pausing/disabling ALSO revokes that agent's widget_tokens
    and api_keys (status='revoked') so a paused agent cannot be used. Revocation is
    permanent — re-activating does not restore old credentials (regenerate new ones)."""
    if status not in ("draft", "active", "paused", "disabled"):
        raise ValueError("status must be one of draft, active, paused, disabled")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET status = %s, updated_at = now() WHERE id = %s",
                (status, agent_id),
            )
            if cur.rowcount == 0:
                raise ValueError("agent not found")
            if status in ("paused", "disabled"):
                cur.execute(
                    "UPDATE widget_tokens SET status = 'revoked' "
                    "WHERE agent_id = %s AND status = 'active'",
                    (agent_id,),
                )
                cur.execute(
                    "UPDATE api_keys SET status = 'revoked' "
                    "WHERE agent_id = %s AND status = 'active'",
                    (agent_id,),
                )
        conn.commit()


def create_widget_token(agent_id: str, allowed_origins: list[str]) -> dict:
    """Generate + store a PUBLIC widget token (safe to return/store in plaintext).
    Returns {id, public_token, allowed_origins}. Raises ValueError if agent is missing."""
    public_token = "wt_" + secrets.token_urlsafe(24)
    origins = list(allowed_origins or [])
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
            if not cur.fetchone():
                raise ValueError("agent not found")
            cur.execute(
                """
                INSERT INTO widget_tokens (agent_id, public_token, allowed_origins)
                VALUES (%s, %s, %s)
                RETURNING id, public_token, allowed_origins
                """,
                (agent_id, public_token, origins),
            )
            row = cur.fetchone()
        conn.commit()
    return {
        "id": _s(row["id"]),
        "public_token": row["public_token"],
        "allowed_origins": list(row["allowed_origins"] or []),
    }


def create_api_key(agent_id: str, scope: str = "ingest") -> dict:
    """Generate a high-entropy API key; store ONLY its sha256 hash. Returns
    {id, api_key, scope} with the PLAINTEXT key — shown ONCE, never stored or logged.
    Raises ValueError on bad scope or missing agent."""
    if scope not in ("admin", "ingest"):
        raise ValueError("scope must be 'admin' or 'ingest'")
    api_key = "sk_" + secrets.token_urlsafe(32)
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
            if not cur.fetchone():
                raise ValueError("agent not found")
            cur.execute(
                """
                INSERT INTO api_keys (agent_id, key_hash, scope)
                VALUES (%s, %s, %s)
                RETURNING id, scope
                """,
                (agent_id, hash_key(api_key), scope),
            )
            row = cur.fetchone()
        conn.commit()
    # NOTE: api_key is the plaintext secret — returned once to the caller, never persisted.
    return {"id": _s(row["id"]), "api_key": api_key, "scope": row["scope"]}


def revoke_widget_token(token_id: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE widget_tokens SET status = 'revoked' WHERE id = %s", (token_id,)
            )
            if cur.rowcount == 0:
                raise ValueError("widget token not found")
        conn.commit()


def revoke_api_key(key_id: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_keys SET status = 'revoked' WHERE id = %s", (key_id,))
            if cur.rowcount == 0:
                raise ValueError("api key not found")
        conn.commit()


# --- Knowledge (content ingestion) --------------------------------------- #
# Staff feed an agent more context through the portal: crawl a website or paste
# text. Every source is PII-redacted (security.redact) BEFORE it is embedded +
# stored (rag.index_document, tenant-scoped RLS), then a knowledge_sources row
# records what was indexed. Chunks live in the tenant-scoped pgvector `chunks`
# table; knowledge_sources is the human-facing audit of ingested sources.
def _agent_tenant(agent_id: str) -> str:
    """Resolve an agent's tenant from agents.config->>'tenant' (defaults to 'demo').

    Raises ValueError if the agent doesn't exist. The tenant is the RLS scope used
    for that agent's vector store (chunks) — the same value db.resolve_widget_token uses.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT config->>'tenant' FROM agents WHERE id = %s", (agent_id,))
            row = cur.fetchone()
    if row is None:
        raise ValueError("agent not found")
    return row[0] or "demo"


def _insert_knowledge_source(
    agent_id: str, kind: str, source: str, status: str, chunk_count: int,
    pii_types: list[str], indexed_at: Optional[datetime],
) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge_sources
                    (agent_id, kind, source, status, chunk_count, pii_types, indexed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (agent_id, kind, source, status, chunk_count, list(pii_types or []), indexed_at),
            )
        conn.commit()


def ingest_agent_url(agent_id: str, url: str, max_pages: int = 8) -> dict:
    """Shallow-crawl `url`, PII-redact + embed each page into the agent's tenant store,
    and record a knowledge_sources row. Returns {pages, indexed_chunks, pii_redacted}.

    The crawl is SSRF-guarded (crawl.crawl) — private/loopback/link-local hosts, non-HTML
    responses and redirects yield no pages. When 0 pages are fetched we record an 'error'
    source row and raise ValueError so the route returns a clean 400 with a helpful message.
    """
    tenant = _agent_tenant(agent_id)
    pages = crawl.crawl(url, max_pages)          # {page_url: text}; SSRF-safe, may be empty
    total = 0
    findings: set = set()
    for page_url, text in pages.items():
        redacted, f = security.redact(text, mode="business")   # keep brand/place; strip contact PII
        total += rag.index_document(tenant, page_url, redacted)
        findings.update(f)
    pii = sorted(findings)
    if not pages:
        _insert_knowledge_source(agent_id, "url", url, "error", 0, pii, None)
        raise ValueError(
            "Couldn't fetch any pages from that URL — it may be unreachable, blocked as "
            "private/internal (SSRF guard), or it served no HTML/text content."
        )
    _insert_knowledge_source(
        agent_id, "url", url, "indexed", total, pii, datetime.now(timezone.utc)
    )
    return {"pages": len(pages), "indexed_chunks": total, "pii_redacted": pii}


def ingest_agent_text(agent_id: str, source: str, text: str) -> dict:
    """PII-redact + embed a pasted block of text into the agent's tenant store and record
    a knowledge_sources row (kind='text'). Returns {indexed_chunks, pii_redacted}."""
    tenant = _agent_tenant(agent_id)
    redacted, findings = security.redact(text, mode="business")   # keep brand/place; strip contact PII
    n = rag.index_document(tenant, source, redacted)
    pii = sorted(findings)
    _insert_knowledge_source(
        agent_id, "text", source, "indexed", n, pii, datetime.now(timezone.utc)
    )
    return {"indexed_chunks": n, "pii_redacted": pii}


def list_agent_knowledge(agent_id: str) -> dict:
    """What's indexed for an agent: {tenant, total_chunks, sources[]}.

    total_chunks counts the agent tenant's rows in the pgvector `chunks` table (read inside
    a tenant-scoped RLS session). NOTE: chunks ingested before this portal feature did NOT
    write knowledge_sources rows, so total_chunks may exceed the sum of the listed sources.
    sources = every knowledge_sources row for this agent, newest first.
    """
    tenant = _agent_tenant(agent_id)
    # Count via the main (RLS-subject) role inside a tenant-scoped session — the read-only
    # role is intentionally NOT granted select on `chunks`, so readonly would return nothing.
    with db.tenant_session(tenant) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks WHERE tenant_id = %s", (tenant,))
            total_chunks = int(cur.fetchone()[0] or 0)
    with db.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT source, kind, status, chunk_count, pii_types, indexed_at, created_at
                FROM knowledge_sources
                WHERE agent_id = %s
                ORDER BY created_at DESC
                """,
                (agent_id,),
            )
            rows = cur.fetchall()
    sources = [
        {
            "source": r["source"],
            "kind": r["kind"],
            "status": r["status"],
            "chunk_count": int(r["chunk_count"] or 0),
            "pii_types": list(r["pii_types"] or []),
            "indexed_at": _iso(r["indexed_at"]),
            "created_at": _iso(r["created_at"]),
        }
        for r in rows
    ]
    return {"tenant": tenant, "total_chunks": total_chunks, "sources": sources}


def audit(
    admin_email: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    diff: Optional[dict] = None,
) -> None:
    """Append an admin action to admin_audit_log. `diff` stored as jsonb.
    Never pass a plaintext api key in `diff` (callers pass ids/metadata only)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_audit_log (admin_email, action, target_type, target_id, diff)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (admin_email, action, target_type, target_id,
                 Json(diff) if diff is not None else None),
            )
        conn.commit()
