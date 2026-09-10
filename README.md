# VELA AI Business-Assistant

A drop-in, **secured, open-source** AI chat widget that answers a business's customer questions —
grounded in that business's own content, 24/7, privacy-safe. The same RAG core also backs Vera,
the voice agent.

**Built and run solo.** I'm Sravan — founder and the only engineer at [VELA](https://byvela.online).
Architecture, RAG pipeline, security model, database, deployment and the embeddable widget are all
mine. This is a production system with paying-customer intent behind it, not a weekend demo: it is
deployed on Cloudflare Containers at `api.byvela.online` and serves the live chat widget on
byvela.online.

### Engineering notes — the decisions worth reading
If you're reviewing this as a work sample, these are the parts with real thinking in them:

- **Anti-hallucination is enforced, not prompted.** Retrieval below a cosine-similarity floor
  (`MIN_SCORE`) short-circuits to an "I don't know" deflection *before* the model is asked, so a
  confident wrong answer is structurally hard rather than discouraged in a system prompt.
- **PII is redacted before the model, not after.** Redaction sits between the user and the LLM, so
  raw contact details never reach the provider, the embeddings, or the logs. Two modes: a regex net
  for a business's own content (keeps brand/place names) and a Presidio NER pass for visitor input.
- **Tenant isolation is enforced by the database.** Postgres Row-Level Security with a
  server-derived `app.tenant_id` GUC — a compromised API key cannot read another tenant's rows,
  because the isolation doesn't depend on application code being correct.
- **Chat-to-SQL is gated by a parser, not a regex.** `app/core/sql_guard.py` parses candidate SQL
  with sqlglot and enforces a function allow-list, a denied-column list, a `SELECT *` block and
  alias-shadowing checks, then executes on a write-denied role inside a read-only transaction with
  a row cap and statement timeout. It was adversarially tested against ~200 payloads; the
  regressions that pass are in `tests/test_sql_guard.py`.
- **SSRF guard on ingestion.** The crawler blocks link-local and private ranges (including cloud
  metadata endpoints), refuses redirects, and caps response size and page count.
- **53 passing tests** (3 skipped, they need live credentials), covering the SQL gate, the RAG/SQL router, privacy paths and integration.

Known limits are documented honestly in `SECURITY.md` rather than omitted.

## What makes it different (the pitch)
- **Grounded.** Answers only from the business's published content and says when it doesn't know; deflects to a human when unsure (→ lead capture). Like any LLM product it can still be wrong — clients review their content.
- **Privacy by design.** Email, phone, card and ID numbers are **masked before the LLM** by a regex net (`business` mode — what the production container runs). An optional `strict` mode adds Microsoft Presidio NER (names, addresses) for tenants that need it. Only the masked text is embedded, sent to the model or logged; names in free text are not masked in `business` mode.
- **Multi-tenant & isolated.** Each business has a `tenant_id` + hashed API key; **Postgres Row-Level Security** makes cross-tenant reads impossible.
- **Audited.** Every message → redaction → retrieval → answer is traced (structured audit log + `conversations` table). No raw PII.
- **Secured Chat-to-SQL.** Ask questions of live data; a sqlglot safety gate + read-only role block anything but a bounded SELECT.
- **Fully-hosted, zero-docker stack.** Google Gemini (chat **and** embeddings) · Supabase Postgres + pgvector · FastAPI. The LLM and embeddings are **swappable via env** (drop in OpenAI, Groq, etc. with no code change).

## Stack
| Piece | Default | Swap via |
|---|---|---|
| Chat LLM | **Gemini** `gemini-flash-lite-latest` | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (→ OpenAI `gpt-4o-mini`, Groq, etc.) |
| Embeddings | **Gemini** `gemini-embedding-001` (768-dim) | `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL` / `EMBED_PROVIDER` |
| Vector store | **Supabase pgvector** | `DATABASE_URL` |
| Relational + logs | **Supabase Postgres** | `DATABASE_URL` |
| Read-only SQL role | `vela_readonly` | `READONLY_DATABASE_URL` |

## Quick start (local, no docker, no daemons)
```bash
# 1. one-shot: creates .venv, installs deps, copies .env, starts uvicorn
./run-local.sh
```
…or manually:
```bash
python3 -m venv .venv                       # or: uv venv --python 3.11 .venv
./.venv/bin/pip install -r requirements.txt  # or: uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                          # then fill in the 3 keys below
./.venv/bin/uvicorn app.main:app --reload
```

### The 3 things you must set in `.env`
1. `LLM_API_KEY` — a **Google Gemini** API key (aistudio.google.com). Any OpenAI-compatible provider works via `LLM_BASE_URL`.
2. `EMBED_API_KEY` — a **Google Gemini** API key (aistudio.google.com).
3. `DATABASE_URL` — your **Supabase** Postgres connection URI.

Then, **once**, paste `supabase_setup.sql` into the Supabase SQL editor and run it. It creates all
tables, indexes, RLS policies, the read-only Chat-to-SQL role, and a small Chinook sample.

## Use it
```bash
# health (works with no keys)
curl localhost:8000/health

# ingest a business's website (redacted + embedded into pgvector, per tenant)
curl -X POST localhost:8000/ingest/url -H "X-API-Key: demo-secret-change-me" \
  -H "Content-Type: application/json" -d '{"url":"https://byvela.online","max_pages":6}'

# ask a grounded question (routes to RAG)
curl -X POST localhost:8000/chat -H "X-API-Key: demo-secret-change-me" \
  -H "Content-Type: application/json" -d '{"message":"What does VELA build?"}'

# ask a DATA question (routes to secured Chat-to-SQL over the Chinook sample)
curl -X POST localhost:8000/ask-data -H "X-API-Key: demo-secret-change-me" \
  -H "Content-Type: application/json" -d '{"message":"Which customers generated the most revenue?"}'
```
Demo page + widget: <http://localhost:8000/demo>

## API
| Endpoint | Purpose |
|---|---|
| `POST /ingest/url` | Shallow-crawl a site → redact → embed → index (per tenant, pgvector) |
| `POST /ingest/text` | Index raw text (e.g. FAQs) |
| `POST /chat` | Ask → grounded, PII-safe answer + sources. Routes doc-questions → RAG, data-questions → Chat-to-SQL |
| `POST /ask-data` | Explicit secured Chat-to-SQL (NL→SQL→safety-gate→read-only exec) |
| `POST /lead` | Opt-in lead capture for the "I don't know" hand-off (the ONE intentional PII store) |
| `GET /health` | Status |

All endpoints except `/health` and `/demo` require `X-API-Key`. See **SECURITY.md**.

## Tests
```bash
./.venv/bin/pytest -q        # offline unit tests (redact, injection, SQL guard) — no keys needed
```
Integration tests (ingest → grounded/deflect, tenant isolation) auto-**skip** unless
`DATABASE_URL` + `LLM_API_KEY` + `EMBED_API_KEY` are set.

## Config
See `.env.example` for every key. Swap the LLM or embeddings by changing env only — no code change.
Anti-hallucination threshold: `MIN_SCORE` (cosine similarity).

## Roadmap
Widget key-proxy (Cloudflare Worker) · streaming (SSE) · onboarding admin · Vera (voice) · billing.
