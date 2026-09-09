# VELA AI Business-Assistant

A drop-in, **secured, open-source** AI chat widget that answers a business's customer questions —
grounded in that business's own content, 24/7, privacy-safe. A sellable VELA product; the same
RAG "brain" plugs into Vera (voice) later.

## What makes it different (the pitch)
- **Grounded.** Answers only from the business's published content and says when it doesn't know; deflects to a human when unsure (→ lead capture). Like any LLM product it can still be wrong — clients review their content.
- **Privacy by design.** Email, phone, card and ID numbers are **masked before the LLM** by a regex net (`business` mode — what the production container runs). An optional `strict` mode adds Microsoft Presidio NER (names, addresses) for tenants that need it. Only the masked text is embedded, sent to the model or logged; names in free text are not masked in `business` mode.
- **Multi-tenant & isolated.** Each business has a `tenant_id` + hashed API key; **Postgres Row-Level Security** makes cross-tenant reads impossible.
- **Audited.** Every message → redaction → retrieval → answer is traced (structured audit log + `conversations` table). No raw PII.
- **Secured Chat-to-SQL.** Ask questions of live data; a sqlglot safety gate + read-only role block anything but a bounded SELECT.
- **Fully-hosted, zero-docker stack.** Groq (chat) · Google Gemini (embeddings) · Supabase Postgres + pgvector · FastAPI. The LLM and embeddings are **swappable via env**.

## Stack
| Piece | Default | Swap via |
|---|---|---|
| Chat LLM | **Groq** `llama-3.1-8b-instant` | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (→ OpenAI `gpt-4o-mini`, etc.) |
| Embeddings | **Gemini** `text-embedding-004` (768-dim) | `EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL` / `EMBED_PROVIDER` |
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
1. `LLM_API_KEY` — a free **Groq** API key (console.groq.com).
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
