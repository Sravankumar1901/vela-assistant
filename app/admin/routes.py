"""Admin portal FastAPI router.

Internal, token-gated ops surface for VELA staff. Auth is a single high-entropy token
(`settings.ADMIN_TOKEN`) presented as the `X-Admin-Token` header and compared in constant
time. If ADMIN_TOKEN is unset the portal fails CLOSED (503), same posture as tenant auth.

The HTML page itself (GET /admin) is served WITHOUT auth (it holds no data); every /admin/api/*
call requires the token. Service ValueErrors become clean 400s — raw DB errors are never leaked.
"""
from __future__ import annotations

import hmac
import html
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..core.config import settings
from . import service

router = APIRouter()

_UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")


# --- Auth ----------------------------------------------------------------- #
def admin_auth(x_admin_token: str = Header(default="")) -> str:
    """Gate every /admin/api/* call. Returns an admin identifier used for the audit log."""
    if not settings.ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="admin portal disabled")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return "admin"


# --- Request bodies (constrained; defense in depth) ----------------------- #
class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    contact_name: Optional[str] = Field(default=None, max_length=200)
    contact_email: Optional[str] = Field(default=None, max_length=320)
    contact_phone: Optional[str] = Field(default=None, max_length=50)
    website: Optional[str] = Field(default=None, max_length=500)
    brand_accent: str = Field(default="#c9a25a", max_length=32)


class AgentIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern=r"^(chat|voice)$")
    name: str = Field(min_length=1, max_length=200)
    plan_id: Optional[str] = Field(default=None, max_length=64)
    config: Optional[dict] = None


class StatusIn(BaseModel):
    status: str = Field(pattern=r"^(draft|active|paused|disabled)$")


class WidgetTokenIn(BaseModel):
    allowed_origins: list[str] = Field(default_factory=list, max_length=50)


class ApiKeyIn(BaseModel):
    scope: str = Field(default="ingest", pattern=r"^(admin|ingest)$")


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class IngestUrlIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    max_pages: int = Field(default=8, ge=1, le=20)


class IngestTextIn(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=50000)


def _bad_request(e: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


# --- HTML page (no auth on the page; its fetches carry the token) --------- #
@router.get("/admin")
def admin_page():
    return FileResponse(_UI_PATH)


# --- Login (NO admin_auth: this is how the portal obtains the token) ------- #
@router.post("/admin/api/login")
def api_login(body: LoginIn):
    """Validate demo/staff credentials and hand back the admin token the rest of
    the portal presents as X-Admin-Token.

    MVP: on success we return settings.ADMIN_TOKEN (the single master token) so the
    SPA can call the token-gated endpoints. HARDENING STEP: mint a short-lived,
    per-session JWT here instead of returning the master token, and have admin_auth
    verify the JWT. Fails closed (503) when ADMIN_TOKEN is unset, matching admin_auth.
    """
    if not settings.ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="admin portal disabled")
    admin = service.verify_admin(body.email, body.password)
    if not admin:
        raise HTTPException(status_code=401, detail="invalid credentials")
    service.audit(admin["email"], "admin.login", "admin_user", None, {"email": admin["email"]})
    return {"token": settings.ADMIN_TOKEN, "email": admin["email"], "role": admin["role"]}


# --- API ------------------------------------------------------------------ #
@router.get("/admin/api/dashboard")
def api_dashboard(_admin: str = Depends(admin_auth)):
    return service.dashboard_metrics()


@router.get("/admin/api/clients")
def api_list_clients(_admin: str = Depends(admin_auth)):
    return service.list_clients()


@router.get("/admin/api/plans")
def api_list_plans(_admin: str = Depends(admin_auth)):
    return service.list_plans()


@router.get("/admin/api/infrastructure")
def api_infrastructure(_admin: str = Depends(admin_auth)):
    """Infra analytics: DB size + per-table breakdown, codebase footprint, Cloudflare
    Pages/Workers (live when configured), and a storage inventory. Read-only — no audit."""
    return service.infrastructure_stats()


@router.post("/admin/api/clients")
def api_create_client(body: ClientIn, admin: str = Depends(admin_auth)):
    try:
        client = service.create_client(
            name=body.name,
            slug=body.slug,
            contact_name=body.contact_name,
            contact_email=body.contact_email,
            contact_phone=body.contact_phone,
            website=body.website,
            brand_accent=body.brand_accent,
        )
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "client.create", "client", client["id"],
                  {"slug": client["slug"], "name": client["name"]})
    return client


@router.post("/admin/api/agents")
def api_create_agent(body: AgentIn, admin: str = Depends(admin_auth)):
    try:
        agent = service.create_agent(
            client_id=body.client_id,
            type=body.type,
            name=body.name,
            plan_id=body.plan_id,
            config=body.config,
        )
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "agent.create", "agent", agent["id"],
                  {"client_id": agent["client_id"], "type": agent["type"], "name": agent["name"]})
    return agent


@router.post("/admin/api/agents/{agent_id}/status")
def api_set_agent_status(body: StatusIn, agent_id: str = Path(max_length=64),
                         admin: str = Depends(admin_auth)):
    try:
        service.set_agent_status(agent_id, body.status)
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "agent.status", "agent", agent_id, {"status": body.status})
    return {"ok": True, "agent_id": agent_id, "status": body.status}


@router.post("/admin/api/agents/{agent_id}/widget-token")
def api_create_widget_token(body: WidgetTokenIn, agent_id: str = Path(max_length=64),
                            admin: str = Depends(admin_auth)):
    try:
        token = service.create_widget_token(agent_id, body.allowed_origins)
    except ValueError as e:
        raise _bad_request(e)
    # public_token is public by design; safe to record the id in the audit trail.
    service.audit(admin, "widget_token.create", "agent", agent_id,
                  {"widget_token_id": token["id"], "allowed_origins": token["allowed_origins"]})
    return token


@router.post("/admin/api/agents/{agent_id}/api-key")
def api_create_api_key(body: ApiKeyIn, agent_id: str = Path(max_length=64),
                       admin: str = Depends(admin_auth)):
    try:
        key = service.create_api_key(agent_id, body.scope)
    except ValueError as e:
        raise _bad_request(e)
    # Audit the id + scope ONLY — never the plaintext api key.
    service.audit(admin, "api_key.create", "agent", agent_id,
                  {"api_key_id": key["id"], "scope": key["scope"]})
    return key


@router.post("/admin/api/widget-tokens/{token_id}/revoke")
def api_revoke_widget_token(token_id: str = Path(max_length=64),
                            admin: str = Depends(admin_auth)):
    try:
        service.revoke_widget_token(token_id)
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "widget_token.revoke", "widget_token", token_id, None)
    return {"ok": True, "token_id": token_id}


@router.post("/admin/api/api-keys/{key_id}/revoke")
def api_revoke_api_key(key_id: str = Path(max_length=64),
                       admin: str = Depends(admin_auth)):
    try:
        service.revoke_api_key(key_id)
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "api_key.revoke", "api_key", key_id, None)
    return {"ok": True, "key_id": key_id}


@router.get("/admin/api/embed-snippet/{agent_id}")
def api_embed_snippet(agent_id: str = Path(max_length=64), _admin: str = Depends(admin_auth)):
    """Ready-to-paste <script> embed for a CHAT agent, using its ACTIVE widget token
    (public token — NEVER an api key) and the agent's config. data-api points at a
    placeholder host for staff to replace."""
    ctx = service.get_embed_context(agent_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if ctx["type"] != "chat":
        return {"snippet": None, "hint": "Embeds are for chat agents only."}
    if not ctx["public_token"]:
        return {"snippet": None,
                "hint": "No active widget token — generate one first, then copy the embed."}

    cfg = ctx["config"] or {}
    accent = cfg.get("accent") or ctx["brand_accent"] or "#c9a25a"
    booking_url = cfg.get("booking_url") or "https://byvela.online/book"
    phone = cfg.get("phone") or ""
    widget_pos = cfg.get("widget_pos") or "right"
    host = "https://YOUR-ASSISTANT-HOST"

    def esc(v: str) -> str:
        return html.escape(str(v), quote=True)

    snippet = (
        f'<script src="{host}/static/widget.js"\n'
        f'        data-api="{host}"\n'
        f'        data-key="{esc(ctx["public_token"])}"\n'
        f'        data-name="{esc(ctx["name"])}"\n'
        f'        data-accent="{esc(accent)}"\n'
        f'        data-book="{esc(booking_url)}"\n'
        f'        data-phone="{esc(phone)}"\n'
        f'        data-pos="{esc(widget_pos)}"></script>'
    )
    return {"snippet": snippet, "public_token": ctx["public_token"]}


# --- Knowledge (content ingestion) ---------------------------------------- #
@router.get("/admin/api/agents/{agent_id}/knowledge")
def api_agent_knowledge(agent_id: str = Path(max_length=64),
                        _admin: str = Depends(admin_auth)):
    """What's indexed for an agent: {tenant, total_chunks, sources[]}."""
    try:
        return service.list_agent_knowledge(agent_id)
    except ValueError as e:
        raise _bad_request(e)


@router.post("/admin/api/agents/{agent_id}/ingest/url")
def api_ingest_url(body: IngestUrlIn, agent_id: str = Path(max_length=64),
                   admin: str = Depends(admin_auth)):
    """Crawl a website and index it into the agent's knowledge base. Synchronous —
    the crawl + embeddings can take 20-90s. SSRF-blocked/unreachable URLs -> 400."""
    try:
        result = service.ingest_agent_url(agent_id, body.url, body.max_pages)
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "knowledge.ingest_url", "agent", agent_id,
                  {"url": body.url, "pages": result["pages"],
                   "indexed_chunks": result["indexed_chunks"],
                   "pii_redacted": result["pii_redacted"]})
    return result


@router.post("/admin/api/agents/{agent_id}/ingest/text")
def api_ingest_text(body: IngestTextIn, agent_id: str = Path(max_length=64),
                    admin: str = Depends(admin_auth)):
    """Index a pasted block of text into the agent's knowledge base."""
    try:
        result = service.ingest_agent_text(agent_id, body.source, body.text)
    except ValueError as e:
        raise _bad_request(e)
    service.audit(admin, "knowledge.ingest_text", "agent", agent_id,
                  {"source": body.source, "indexed_chunks": result["indexed_chunks"],
                   "pii_redacted": result["pii_redacted"]})
    return result
