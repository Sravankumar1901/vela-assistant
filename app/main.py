"""VELA AI Business-Assistant — secured RAG chat API + embeddable widget.

Flow: auth -> rate-limit -> sanitize -> injection-flag -> PII-redact -> retrieve
      (tenant-isolated, pgvector) -> grounded answer -> audit. PII never reaches the LLM.

Stack: Groq (chat) + Gemini (embeddings) + Supabase Postgres/pgvector. Zero docker.
"""
import json
import re
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .admin import routes as admin_routes
from .core import chatsql, db, llm, rag, security
from .core.config import settings
from .ingest import crawl

app = FastAPI(title="VELA AI Business-Assistant", version="0.2.0")

# Widget is embedded cross-origin on client sites; API-key/widget-token auth gates
# access. In production set ALLOWED_ORIGINS to the exact site origin(s) so the public
# widget token can't be exercised from an attacker's page (origin-lock).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list(),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Internal, token-gated admin portal (/admin). Gated by settings.ADMIN_TOKEN; fails closed.
app.include_router(admin_routes.router)


@app.on_event("startup")
def _startup():
    # Best-effort: upsert bootstrap seed tenants (hashed keys) into the DB.
    try:
        n = db.bootstrap_seed_tenants()
        if n:
            print(f"[startup] seeded/updated {n} tenant(s) from TENANT_KEYS")
    except Exception as e:  # DB not reachable yet — auth falls back to env seed
        print(f"[startup] tenant seed skipped: {e}")


def auth(x_api_key: str = Header(default="")) -> str:
    tenant = security.resolve_tenant(x_api_key)
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if security.rate_limited(tenant):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return tenant


def _request_origin(origin: str, referer: str) -> str:
    """Derive the request's `scheme://host[:port]` from the Origin header, falling back to
    the scheme+host of Referer. Returns "" when neither is present."""
    src = (origin or "").strip() or (referer or "").strip()
    if not src:
        return ""
    parts = urlsplit(src)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return src.rstrip("/")


def _origin_allowed(origin: str, allowed_origins: list[str]) -> bool:
    """True if `origin` is in the allow-list (lenient on trailing slash + case).
    An empty allow-list means the widget is open to any origin."""
    if not allowed_origins:
        return True

    def _norm(s: str) -> str:
        return (s or "").strip().rstrip("/").lower()

    o = _norm(origin)
    return bool(o) and any(_norm(a) == o for a in allowed_origins)


def widget_auth(
    x_widget_token: str = Header(default=""),
    origin: str = Header(default=""),
    referer: str = Header(default=""),
) -> str:
    """Auth for the PUBLIC, read-only widget proxy. Resolves a portal-issued widget token
    (wt_...) to its tenant, enforcing the token's origin allow-list + per-token rate limit.

    401 on missing/invalid/revoked token or paused/disabled agent; 403 on a disallowed origin.
    """
    token = (x_widget_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing widget token")
    rec = db.resolve_widget_token(token)
    if (not rec) or rec["wt_status"] != "active" or rec["agent_status"] in ("paused", "disabled"):
        raise HTTPException(status_code=401, detail="Invalid widget token")
    req_origin = _request_origin(origin, referer)
    if not _origin_allowed(req_origin, rec["allowed_origins"]):
        raise HTTPException(status_code=403, detail="origin not allowed")
    # Per-token rate limit (reuses the in-memory bucket, keyed by the public token).
    if security.rate_limited(token):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return rec["tenant"]


class IngestText(BaseModel):
    source: str
    text: str


class IngestUrl(BaseModel):
    url: str
    max_pages: int = 8


class ChatIn(BaseModel):
    message: str


class WidgetChatIn(BaseModel):
    # Public widget proxy input — length-constrained (sanitize_user_input also truncates to 2000).
    message: str = Field(min_length=1, max_length=4000)


class AskDataIn(BaseModel):
    message: str


class LeadIn(BaseModel):
    question: str
    contact: str  # opt-in: the ONE intentional PII field (see leads table)


@app.get("/health")
def health():
    return {
        "ok": True,
        "llm_model": settings.LLM_MODEL,
        "embed_model": settings.EMBED_MODEL,
        "embed_dim": settings.EMBED_DIM,
        "vector_store": "supabase-pgvector",
        "db_configured": db.available(),
        "pii_redaction": settings.PII_REDACTION,
    }


@app.post("/ingest/text")
def ingest_text(body: IngestText, tenant: str = Depends(auth)):
    redacted, findings = security.redact(body.text, mode="business")  # keep brand/place names
    n = rag.index_document(tenant, body.source, redacted)
    security.audit({"event": "ingest", "tenant": tenant, "source": body.source,
                    "chunks": n, "pii_findings": findings})
    return {"indexed_chunks": n, "pii_redacted": findings}


@app.post("/ingest/url")
def ingest_url(body: IngestUrl, tenant: str = Depends(auth)):
    pages = crawl.crawl(body.url, max_pages=body.max_pages)
    total, all_findings = 0, set()
    for url, text in pages.items():
        redacted, findings = security.redact(text, mode="business")
        total += rag.index_document(tenant, url, redacted)
        all_findings.update(findings)
    security.audit({"event": "ingest_url", "tenant": tenant, "base": body.url,
                    "pages": len(pages), "chunks": total, "pii_findings": sorted(all_findings)})
    return {"pages": len(pages), "indexed_chunks": total, "pii_redacted": sorted(all_findings)}


# Heuristic router: Chat-to-SQL only when a question is BOTH quantitative AND about the
# live-data schema; everything about the business itself goes to the knowledge base.
# (Bug 2026-09-07: "how many types of business websites can you build" matched "how many"
# alone → SQL → the model wrote `SELECT 0 AS result` → the visitor saw "result 0".)
_DATA_HINT = re.compile(
    r"\b(how many|count|number of|total|revenue|sales|top|most|least|average|avg|sum|"
    r"highest|lowest|per (customer|country|genre|artist)|rank|list all|which (customer|artist|album|track|genre))\b",
    re.IGNORECASE,
)
# Something the schema actually knows about must be named (see chatsql.SCHEMA_HINT).
_DATA_ENTITY = re.compile(
    r"\b(customers?|invoices?|invoice items?|tracks?|albums?|artists?|genres?|revenue|sales|"
    r"billing|purchases?|orders?|spend(ing|ers)?|buyers?|countr(y|ies))\b",
    re.IGNORECASE,
)
# Vocabulary of "questions about VELA" — never a data lookup even if phrased with "how many".
_KB_VOCAB = re.compile(
    r"\b(websites?|sites?|web|build|design|pric(e|es|ing)|costs?|plans?|packages?|vera|"
    r"assistant|receptionist|chat(bot)?|services?|book(ing)?|deliver(y)?|timeline|turnaround|"
    r"portfolio|concepts?|demos?|industr(y|ies)|types? of|kinds? of|vela)\b",
    re.IGNORECASE,
)


def _looks_like_data_question(text: str) -> bool:
    t = text or ""
    if not _DATA_HINT.search(t) or not _DATA_ENTITY.search(t):
        return False
    return not _KB_VOCAB.search(t)


def _sql_result_is_useless(data: dict) -> bool:
    """True when Chat-to-SQL ran but produced nothing a visitor can use — blocked, no rows,
    a schema-less constant (`SELECT 0`), or a lone null/zero scalar — so the caller falls
    back to the knowledge base instead of answering "result 0"."""
    if not data.get("executed"):
        return True
    rows = data.get("rows") or []
    if not rows:
        return True
    sql = f" {(data.get('safe_sql') or '').upper()} "
    if " FROM " not in sql:
        return True
    if len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] in (None, 0, "", "0"):
        return True
    return False


def run_chat(tenant: str, message: str) -> dict:
    """Core /chat logic, shared by the API-key endpoint and the widget proxy.

    Returns the exact dict /chat returns. `tenant` is already resolved/authorized by the caller.
    """
    raw = security.sanitize_user_input(message)
    injection = security.injection_flag(raw)
    redacted, findings = security.redact(raw)              # PII stripped BEFORE retrieval/LLM

    route = "sql" if _looks_like_data_question(redacted) else "rag"
    data: dict = {}
    if route == "sql":
        data = chatsql.run(tenant, redacted)
        if _sql_result_is_useless(data):
            # Nothing usable came back — answer from the knowledge base instead.
            security.audit({"event": "chat_route_fallback", "tenant": tenant,
                            "safe_sql": data.get("safe_sql"), "row_count": data.get("row_count"),
                            "blocked_reason": data.get("blocked_reason")})
            route = "rag"

    if route == "sql":
        grounded = True
        sources = ["live-data:sql"]
        answer = _format_sql_answer(data)  # strict-redacted inside
        stored_answer = _sql_summary(data)  # conversations.answer never receives rows
        security.audit({"event": "chat", "route": "sql", "tenant": tenant,
                        "question_redacted": redacted, "pii_findings": findings,
                        "injection_flag": injection, "grounded": grounded,
                        "generated_sql": data.get("generated_sql"), "safe_sql": data.get("safe_sql"),
                        "executed": data.get("executed"), "blocked_reason": data.get("blocked_reason"),
                        "row_count": data.get("row_count")})
    else:
        hits = rag.retrieve(tenant, redacted)
        grounded = len(hits) > 0
        sources = sorted({h["source"] for h in hits})
        if not grounded:
            answer = ("I don't have that information yet — I can connect you with the team who can help. "
                      "Would you like to leave your question?")
        else:
            answer = llm.answer(redacted, [h["text"] for h in hits])
        stored_answer = answer
        security.audit({"event": "chat", "route": "rag", "tenant": tenant,
                        "question_redacted": redacted, "pii_findings": findings,
                        "injection_flag": injection, "grounded": grounded,
                        "sources": [h["source"] for h in hits]})

    # Persist the turn to Postgres (RLS-scoped). NO raw PII; SQL routes store a summary only.
    try:
        db.log_conversation(tenant, redacted, stored_answer, grounded, sources, findings, injection)
    except Exception as e:
        print(f"[chat] conversation log skipped: {e}")

    return {
        "answer": answer,
        "grounded": grounded,
        "route": route,
        "sources": sources,
        "pii_redacted": findings,
        "injection_flagged": injection,
    }


@app.post("/chat")
def chat(body: ChatIn, tenant: str = Depends(auth)):
    return run_chat(tenant, body.message)


def _chunk_text(text: str, size: int = 22):
    """Yield a static string in small pieces so non-LLM answers also 'stream'."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _split_answer(full_text: str):
    """Split the structured reply into (lead answer, key_points[]) — bullets become typed points."""
    lead, key_points = [], []
    for line in (full_text or "").splitlines():
        s = line.strip()
        if s[:2] == "- " or (s[:1] == "-" and s[1:2] == " "):
            key_points.append(s[2:].strip())
        elif s and not key_points:  # lead text appears before any bullets
            lead.append(s)
    answer = " ".join(lead).strip() or (full_text or "").strip()
    return answer, key_points


def _suggested_action(route: str, grounded: bool, needs_human: bool) -> str:
    if needs_human:
        return "Talk to the team"
    if route == "sql":
        return "Ask another data question"
    return "Book a call"


def stream_chat(tenant: str, message: str) -> StreamingResponse:
    """Core /chat/stream logic, shared by the API-key endpoint and the widget proxy.

    Emits `event: token` deltas as the answer is generated, then one `event: result`
    carrying the strict typed JSON (answer, key_points, sources, suggested_action, needs_human),
    then `event: done`. PII is redacted before retrieval/LLM, exactly like /chat.
    `tenant` is already resolved/authorized by the caller.
    """
    raw = security.sanitize_user_input(message)
    injection = security.injection_flag(raw)
    redacted, findings = security.redact(raw)
    initial_route = "sql" if _looks_like_data_question(redacted) else "rag"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    def gen():
        full, sql_meta, stored_answer = "", {}, None
        route, data = initial_route, {}
        if route == "sql":
            data = chatsql.run(tenant, redacted)
            if _sql_result_is_useless(data):
                # Nothing usable came back — answer from the knowledge base instead.
                security.audit({"event": "chat_route_fallback", "tenant": tenant,
                                "safe_sql": data.get("safe_sql"), "row_count": data.get("row_count"),
                                "blocked_reason": data.get("blocked_reason")})
                route = "rag"
        if route == "sql":
            grounded = True
            sources = ["live-data:sql"]
            # _format_sql_answer is strict-redacted BEFORE the first token is streamed.
            text = _format_sql_answer(data)
            stored_answer = _sql_summary(data)  # conversations.answer never receives rows
            sql_meta = {"safe_sql": data.get("safe_sql"), "executed": True,
                        "blocked_reason": None, "row_count": data.get("row_count")}
            for piece in _chunk_text(text):
                full += piece
                yield sse("token", {"text": piece})
        else:
            hits = rag.retrieve(tenant, redacted)
            grounded = len(hits) > 0
            sources = sorted({h["source"] for h in hits})
            if not grounded:
                text = ("I don't have that information yet — I can connect you with the team who can help. "
                        "Would you like to leave your question?")
                for piece in _chunk_text(text):
                    full += piece
                    yield sse("token", {"text": piece})
            else:
                try:
                    for delta in llm.answer_stream(redacted, [h["text"] for h in hits]):
                        full += delta
                        yield sse("token", {"text": delta})
                except Exception as e:
                    print(f"[chat_stream] llm error: {e}")
                    fallback = "Sorry — I hit a temporary limit. Please try again in a moment."
                    if not full:
                        full = fallback
                        yield sse("token", {"text": fallback})

        answer, key_points = _split_answer(full)
        if stored_answer is None:  # RAG route: the answer itself is what we persist
            stored_answer = answer
        needs_human = (not grounded) or injection
        result = {
            "answer": answer,
            "key_points": key_points,
            "sources": sources,
            "suggested_action": _suggested_action(route, grounded, needs_human),
            "needs_human": needs_human,
            "grounded": grounded,
            "route": route,
            "pii_redacted": findings,
            "injection_flagged": injection,
        }
        if sql_meta:
            result["sql"] = sql_meta
        yield sse("result", result)
        yield sse("done", {})

        security.audit({"event": "chat_stream", "route": route, "tenant": tenant,
                        "question_redacted": redacted, "pii_findings": findings,
                        "injection_flag": injection, "grounded": grounded, "sources": sources})
        try:
            db.log_conversation(tenant, redacted, stored_answer, grounded, sources, findings, injection)
        except Exception as e:
            print(f"[chat_stream] conversation log skipped: {e}")

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/stream")
def chat_stream(body: ChatIn, tenant: str = Depends(auth)):
    """Streaming chat (SSE) for API-key clients. See stream_chat for the response contract."""
    return stream_chat(tenant, body.message)


@app.post("/ask-data")
def ask_data(body: AskDataIn, tenant: str = Depends(auth)):
    """Explicit secured Chat-to-SQL endpoint."""
    raw = security.sanitize_user_input(body.message)
    injection = security.injection_flag(raw)
    redacted, findings = security.redact(raw)

    data = chatsql.run(tenant, redacted)
    executed = bool(data.get("executed"))
    answer = _format_sql_answer(data) if executed else (  # strict-redacted inside
        "That request was blocked by the SQL safety gate or the DB is unavailable.")

    security.audit({"event": "ask_data", "tenant": tenant, "question_redacted": redacted,
                    "pii_findings": findings, "injection_flag": injection,
                    "generated_sql": data.get("generated_sql"), "safe_sql": data.get("safe_sql"),
                    "executed": executed, "blocked_reason": data.get("blocked_reason"),
                    "row_count": data.get("row_count")})
    try:
        # Persist the summary (row_count + safe_sql), never the rows.
        db.log_conversation(tenant, redacted, _sql_summary(data), executed, ["live-data:sql"],
                            findings, injection)
    except Exception as e:
        print(f"[ask-data] conversation log skipped: {e}")

    return {
        "answer": answer,
        "executed": executed,
        "blocked_reason": data.get("blocked_reason"),
        "safe_sql": data.get("safe_sql"),
        "columns": data.get("columns"),
        "rows": data.get("rows"),
        "row_count": data.get("row_count"),
        "pii_redacted": findings,
        "injection_flagged": injection,
    }


@app.post("/lead")
def lead(body: LeadIn, tenant: str = Depends(auth)):
    """Opt-in lead capture for the 'I don't know' hand-off.

    `contact` is the ONE intentional PII store (leads.contact). The question is redacted;
    the contact is stored as the visitor explicitly provided it (see supabase_setup.sql).
    """
    redacted, _ = security.redact(body.question)
    try:
        db.capture_lead(tenant, redacted, body.contact)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"lead capture unavailable: {e}")
    # Audit records the redacted question only — NOT the contact PII.
    security.audit({"event": "lead", "tenant": tenant, "question_redacted": redacted, "captured": True})
    return {"captured": True}


def _format_sql_answer(data: dict) -> str:
    """Visitor-facing text for an executed Chat-to-SQL result — always redacted (business mode).

    The redaction lives here (not at the call sites) so every path that renders rows — /chat,
    /chat/stream, /ask-data and the /widget/* proxies — gets it before anything is streamed,
    returned or logged. Defense in depth behind the sql_guard column deny-list: even if a
    non-allow-listed value carries an email/phone/name, it never leaves the process raw.
    """
    cols = data.get("columns") or []
    rows = data.get("rows") or []
    if not rows:
        return "No matching records were found."
    if len(rows) == 1 and len(cols) == 1:
        # A single figure reads better as a sentence than as a one-cell table.
        text = f"{cols[0].replace('_', ' ').capitalize()}: {rows[0][0]}"
    else:
        lines = [" | ".join(cols)]
        for r in rows[:20]:
            lines.append(" | ".join("" if v is None else str(v) for v in r))
        more = "" if len(rows) <= 20 else f"\n… and {len(rows) - 20} more rows."
        text = "Here's what I found:\n" + "\n".join(lines) + more
    # Regex net only (email/phone/card/SSN): sql_guard's column deny-list already keeps
    # names out of results, and strict/NER mode would mangle legitimate values such as
    # country or artist names in "revenue by country" answers.
    redacted, _ = security.redact(text, mode="business")
    return redacted


def _sql_summary(data: dict) -> str:
    """What conversations.answer stores for a SQL-routed turn: row_count + the gated SQL.

    Never the rows. safe_sql is regenerated from the AST of SQL the LLM wrote from an
    already-redacted question, so it carries no visitor PII either. (Audit fix: "persist
    only row_count and safe_sql for SQL routes".)
    """
    if data.get("executed"):
        return (f"[chat-to-sql] executed; row_count={int(data.get('row_count') or 0)}; "
                f"safe_sql={data.get('safe_sql')}")
    return f"[chat-to-sql] not executed; blocked_reason={data.get('blocked_reason')}"


# ---- Public widget proxy (READ-ONLY: chat only, NO ingest) ----
# Authenticated by a portal-issued widget token (wt_...) + origin allow-list, NOT the
# write-capable tenant API key. Closes audit finding H1 (embeds no write credential in the browser).
@app.post("/widget/chat/stream")
def widget_chat_stream(body: WidgetChatIn, tenant: str = Depends(widget_auth)):
    """Streaming chat for the embeddable widget. Same SSE contract as /chat/stream."""
    return stream_chat(tenant, body.message)


@app.post("/widget/chat")
def widget_chat(body: WidgetChatIn, tenant: str = Depends(widget_auth)):
    """Non-streaming chat for the embeddable widget. Same JSON shape as /chat."""
    return run_chat(tenant, body.message)


# ---- Widget + demo (static) ----
app.mount("/static", StaticFiles(directory="app/widget"), name="static")


@app.get("/demo")
def demo():
    return FileResponse("app/widget/demo.html")
