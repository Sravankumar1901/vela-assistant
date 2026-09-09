# VELA AI Business-Assistant — Development Handover Plan

**For:** the developing agent/engineer picking this up.
**Owner/approver:** Sravan (do NOT deploy anything public without his explicit OK).
**Repo:** `~/Documents/partner/vela-assistant/` · **Scope doc:** `../documents/proof-project-secured-rag-scope.md`
**Last updated:** 2026-09-06

---

## 1. What you're building (and why)
A **sellable, secured, open-source AI chat widget** that answers a business's customer questions,
grounded in that business's own content, privacy-safe. Multi-tenant. The same RAG core is designed to
later power Vera (VELA's voice receptionist).

It serves three goals — keep all three intact:
1. A **sellable VELA product** (drop-in widget, live demo on byvela.online).
2. **Portfolio proof** for the founder's freelance bids (public GitHub repo + Loom).
3. Demonstrates **productionized RAG + PII redaction + (later) Chat-to-SQL**.

## 2. Current state — WHAT ALREADY EXISTS (build ON this, don't rewrite)
The core is scaffolded and **compiles clean** but has **NOT been run live yet** — expect runtime bugs to fix in Phase 0.

```
vela-assistant/
  app/
    main.py                # FastAPI: /health /ingest/url /ingest/text /chat  (secured flow wired)
    core/
      config.py            # env-driven settings
      security.py          # Presidio PII redaction (+regex fallback), injection detect, API-key auth, rate-limit, audit(JSONL+optional Langfuse)
      rag.py               # per-tenant Qdrant collections, chunking, embed+retrieve, MIN_SCORE anti-hallucination
      llm.py               # OpenAI-compatible client (Ollama default), grounded-answer system prompt
    ingest/crawl.py        # website crawler + HTML->text (requests+bs4)
    widget/
      widget.js            # drop-in floating chat bubble (<script> embed)
      demo.html            # demo/sell page served at /demo
  docker-compose.yml       # qdrant + ollama + app (+ optional langfuse under --profile obs)
  Dockerfile               # python:3.11-slim + spaCy en_core_web_lg
  requirements.txt
  .env.example             # all config keys
  scripts/pull-models.sh   # pulls llama3.1:8b + nomic-embed-text into Ollama
  README.md  SECURITY.md
```

## 3. Architecture (target)
```
[widget.js on client site] --HTTPS--> [Cloudflare Worker key-proxy] --> [FastAPI app]
                                                                          |-- Presidio redact (pre-LLM)
                                                                          |-- Qdrant Cloud (per-tenant vectors)
                                                                          |-- Postgres/Supabase (tenants, logs, RLS)
                                                                          |-- LLM (Groq free  <-> gpt-4o-mini)  [PII already stripped]
                                                                          |-- audit log (JSONL / Langfuse)
```

## 4. NON-NEGOTIABLE requirements (must hold in every phase)
- **PII is redacted BEFORE embedding and BEFORE any LLM call.** Raw PII is never embedded, sent to an LLM, or written to any log. (Audit logs store redacted text + PII *types* only.)
- **Tenant isolation** — a tenant can never read another tenant's data (separate Qdrant collections; Postgres Row-Level Security).
- **Grounded answers only** — below `MIN_SCORE` similarity, return "I don't know + offer human hand-off", never invent hours/prices/policies.
- **Open-source stack**, self-hostable. **LLM swappable via env** (free Groq ↔ paid gpt-4o-mini) with zero code change.
- **Secrets only via env / secret manager.** Never commit `.env`. No hardcoded keys.
- **No public deploy without Sravan's explicit approval.**

---

## 5. Phased task breakdown (each task has an ACCEPTANCE CRITERION)

### PHASE 0 — Make the existing scaffold run locally (fix runtime bugs)
0.1 `cp .env.example .env`; `docker compose up -d --build`; `./scripts/pull-models.sh`.
    **AC:** all containers healthy; `GET /health` returns `{"ok":true,...}`.
0.2 Fix any runtime errors surfaced (likely: Presidio/spaCy model load, Qdrant vector-dim mismatch with the embedding model, Ollama model names, OpenAI-SDK embedding call shape for Ollama). Verify embedding dim matches `EMBED_DIM` (nomic-embed-text = 768).
    **AC:** `POST /ingest/url {"url":"https://byvela.online","max_pages":6}` returns indexed_chunks>0 and `pii_redacted` list.
0.3 Verify the chat flow end-to-end.
    **AC:** `POST /chat {"message":"What does VELA build?"}` returns a grounded answer + `sources`; an off-topic question returns the "I don't know" deflection; a message containing a fake email/phone shows those types in `pii_redacted`; an "ignore your instructions…" message sets `injection_flagged:true`. Audit lines appear in `/data/audit.log.jsonl` with NO raw PII.
0.4 Verify `/demo` loads and the widget chats.
    **AC:** open `http://localhost:8000/demo`, ask a question via the bubble, get an answer.

### PHASE 1 — Productionize the core (Postgres + logging + swappable LLM)
1.1 Add **Postgres (Supabase-compatible)** as the source of truth for **tenants** (id, name, api_key_hash, accent, created_at) — replace the env `TENANT_KEYS` map with a DB lookup (keep env as a bootstrap seed). Store **hashed** API keys.
    **AC:** a tenant added in the DB can auth and chat; removing it revokes access; API keys are stored hashed, never plaintext.
1.2 Enable **Supabase Row-Level Security** so tenant rows/logs are isolated at the DB.
    **AC:** a query as tenant A cannot read tenant B's rows (documented test).
1.3 **Conversation logging** table (tenant_id, ts, question_redacted, answer, grounded, sources, pii_types, injection_flag). NO raw PII.
    **AC:** every /chat writes one row; a manual inspection shows zero raw PII.
1.4 **Lead capture**: when grounded=false, expose an optional follow-up that captures a visitor question/contact into a `leads` table (contact fields are the ONE place PII is intentionally stored — encrypt at rest / mark sensitive, and it's opt-in by the visitor).
    **AC:** a "don't know" flow can capture a lead; leads are tenant-scoped.
1.5 **Swappable LLM**: confirm `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` switch between Ollama, **Groq** (free), and **OpenAI gpt-4o-mini** with no code change. Add a `LLM_PROVIDER` note in README.
    **AC:** flipping env from Ollama→Groq→gpt-4o-mini works; PII redaction still runs before the call in all three.
1.6 **Streaming** responses (SSE) for better UX (optional but recommended).
    **AC:** widget shows tokens streaming.

### PHASE 2 — Widget key-proxy (close the SECURITY.md gap)
2.1 Build a **Cloudflare Worker** (or a FastAPI proxy route) that holds the tenant key server-side; the widget calls the Worker with only a public site-token; the Worker adds `X-API-Key` and forwards to the app. Rotate/scope public tokens per domain (check `Origin`/referrer allow-list).
    **AC:** page source contains NO tenant secret; requests from a non-allow-listed origin are rejected.
2.2 Host `widget.js` + `demo.html` on Cloudflare Pages (free, CDN, DDoS).
    **AC:** widget loads from Cloudflare; demo page live (staging).

### PHASE 3 — Chat-to-SQL (the second skill-gap closer)
3.1 Add a **Postgres sample DB** (Chinook) as a per-tenant "live data" source (config: connection string per tenant).
3.2 `/ask-data` endpoint: NL→SQL via the LLM, then a **hard safety gate before execution**:
    - execute as a **read-only DB role** (SELECT-only grants),
    - **sqlglot** AST validation: reject non-SELECT, reject multiple statements/`;`, enforce table/column allow-list, force a `LIMIT`,
    - statement **timeout** + row cap, parameterized, no raw string exec,
    - full audit (question_redacted → generated SQL → executed? → rows returned).
    **AC:** "Which customers generated the most revenue last year?" returns a correct answer; an injected `DROP TABLE`/`; DELETE` attempt is **blocked before execution** and audited; a write attempt fails at the read-only role even if validation were bypassed.
3.3 Route in `/chat`: decide docs-RAG vs. data-SQL (simple classifier or explicit endpoints).
    **AC:** doc questions hit RAG, data questions hit Chat-to-SQL.

### PHASE 4 — Minimal admin / onboarding
4.1 A small admin (FastAPI routes or a lightweight page, auth-gated) to: add a tenant, trigger ingest for a URL, view recent audit/conversations.
    **AC:** VELA can onboard a new business (add tenant → ingest their site → get an embed snippet) without editing code.

### PHASE 5 — Deploy (free tier) + demo + portfolio  [Sravan approval required before public]
5.1 Deploy: **Cloud Run** (app) + **Qdrant Cloud** (free) + **Supabase** (free) + **Groq** (free LLM), OR the **Oracle Always-Free VM** self-hosting the whole stack. Secrets in the platform secret manager. TLS everywhere.
    **AC:** app reachable over HTTPS; DBs private; env secrets not in code.
5.2 Live demo on **byvela.online/assistant** (or a subpath), ingested on VELA's own content.
    **AC:** public demo answers questions about VELA, grounded, with the widget.
5.3 Polish **README.md** + **SECURITY.md**, add an **architecture diagram**, record a **2-min Loom** (ask question → grounded answer + PII redacted + blocked injection + blocked SQL). Push to a **public GitHub repo**.
    **AC:** repo is public, `docker compose up` works from a clean clone, Loom link in README.
5.4 One-page **sell-sheet** for SMB outreach (what it is, privacy story, price).
    **AC:** sell-sheet exists in `/docs`.

---

## 6. Testing (add under `tests/`)
- **Unit:** `redact()` catches email/phone/card/SSN and returns types; `injection_flag()` true/false cases; sqlglot validator accepts a SELECT, rejects DROP/DELETE/multi-statement/no-LIMIT.
- **Isolation:** tenant A cannot retrieve tenant B's chunks; RLS blocks cross-tenant rows.
- **Integration:** ingest a fixture doc → ask an in-doc question (grounded) → ask an out-of-doc question (deflection) → confirm audit has no raw PII.
- **Injection suite:** a list of prompt-injection + SQL-injection payloads that must all be blocked/flagged.
**AC:** `pytest` green; injection suite 100% blocked.

## 7. Config reference (`.env`) — see `.env.example`
`TENANT_KEYS` (bootstrap) · `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` · `EMBED_MODEL/EMBED_DIM` · `QDRANT_URL/QDRANT_API_KEY` · `DATABASE_URL` (add) · `PII_REDACTION` · `RATE_LIMIT_PER_MIN` · `TOP_K` · `MIN_SCORE` · `LANGFUSE_*` · `AUDIT_LOG_PATH`.

## 8. Definition of Done (whole project)
- Local `docker compose up` from a clean clone → working demo (Phase 0).
- Multi-tenant with Postgres + RLS, conversation logging, swappable LLM (Phase 1).
- Widget key never client-side (Phase 2).
- Secured Chat-to-SQL passing the injection suite (Phase 3).
- Onboard-a-tenant admin flow (Phase 4).
- Deployed on free tier over HTTPS + live VELA demo + public repo + Loom + sell-sheet (Phase 5, after approval).
- All NON-NEGOTIABLE requirements (§4) hold; `pytest` green.

## 9. Constraints & gotchas
- **Never deploy public / point a real domain without Sravan's explicit OK.**
- **Never log raw PII** anywhere. Audit = redacted text + PII types only. The `leads` table is the sole intentional PII store (opt-in, encrypt at rest).
- Keep the stack **open-source + self-hostable**; the LLM must stay swappable to a local model for privacy-sensitive clients.
- Free-tier sizing is fine for ~10 SMB tenants for storage/compute; the LLM is the only piece that tips into small cost at volume — keep it env-swappable.
- Ollama models are large (~5 GB) — the Docker build + pull is slow the first time; that's expected.
- Widget is embedded cross-origin — keep CORS + the origin allow-list correct.

## 10. Handoff notes
- Start at **Phase 0** and get it green before touching Phase 1+.
- Ask Sravan before: any public deploy, choosing managed-free vs Oracle-VM hosting, and picking the paid-LLM threshold.
- Reference `SECURITY.md` for the intended security model; keep it updated as you build.
