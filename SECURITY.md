# Security & Privacy Design

The secured flow is the product's headline. Every customer message goes through:

```
auth (hashed API key) → rate-limit → sanitize → injection-flag → PII redaction
  → tenant-isolated retrieval (pgvector + RLS) → grounded answer → audit
```

## Controls

1. **PII redaction before the LLM.** `core/security.redact()` runs on every ingested document AND
   every incoming question *before* embedding or the model call. Two modes:
   - `business` (default; what the production Cloudflare container ships — no Presidio installed):
     a regex net that masks email / phone / credit-card / SSN-Aadhaar-style ID numbers with typed
     placeholders. Person names and addresses in free text are **not** masked in this mode.
   - `strict`: Microsoft Presidio NER (names, locations, etc.) + the same regex net on top.
   Only the masked text is embedded, sent to the LLM or logged. Scope is stated honestly on the
   public privacy policy (byvela.online/privacy): "email, phone, card and ID numbers are masked".

2. **Swappable model, redaction always first.** Chat = Groq by default (`gpt-4o-mini` etc. via env);
   embeddings = Gemini. PII is redacted before ANY external call in all paths, so customer PII never
   leaves the box regardless of which provider is configured.

3. **Tenant isolation — `tenant_id` + Postgres Row-Level Security.** Each business = a `tenant_id` and
   a hashed API key. `chunks`, `conversations`, and `leads` have RLS **enabled and FORCED**. Before any
   tenant read/write the app runs `set_config('app.tenant_id', '<tenant>', false)`; every RLS policy is
   `tenant_id = current_setting('app.tenant_id', true)`. A session scoped to tenant A physically cannot
   read tenant B's vectors, conversations, or leads. Queries ALSO filter by `tenant_id` (defense in depth).
   *(Enforcement note: connect with a role subject to RLS — not a BYPASSRLS superuser. `FORCE ROW LEVEL
   SECURITY` covers the table owner too.)*

4. **Anti-hallucination grounding.** Retrieval uses cosine similarity (`1 - (embedding <=> q)`); hits
   below `MIN_SCORE` are dropped. With no qualifying context the assistant says it doesn't know and
   offers a human hand-off (→ opt-in lead capture) rather than inventing hours/prices/policies.

5. **Secured Chat-to-SQL** (`/ask-data`, and data-routed `/chat`). NL→SQL via the LLM, then a **hard
   gate before execution** (`core/sql_guard.validate_and_fix`):
   - sqlglot AST: reject anything that isn't a single SELECT; reject multiple statements / stray `;`;
     reject SELECT…INTO and any DML/DDL node anywhere in the tree;
   - enforce a **table + column allow-list** (Chinook subset);
   - **force/clamp a LIMIT** (`SQL_ROW_CAP`);
   - the SQL that executes is **regenerated from the AST** (strips comments and any injected tail).
   Execution runs on a **read-only DB role** (`READONLY_DATABASE_URL` → `vela_readonly`, SELECT-only
   grants) inside a `READ ONLY` transaction with a `statement_timeout` and a row cap. Even if the gate
   were bypassed, a write fails at the role. Full audit: question_redacted → generated SQL → executed? →
   row count (no raw PII).

6. **Prompt-injection defenses.** `injection_flag()` detects override attempts; the system prompt treats
   all context and user input as untrusted data, never as instructions. Injection attempts are flagged
   in the audit log and the `conversations` table.

7. **Auth, rate limiting, input caps.** API keys are stored as **sha256 hashes** in the `tenants` table
   (never plaintext); `X-API-Key` is hashed and compared. Removing a tenant row revokes access. Per-tenant
   per-minute rate limit, input length cap, null-byte stripping.

8. **Audit trail.** `audit()` writes an append-only JSONL record (redacted question, PII **types**,
   injection flag, grounded?, sources, and for SQL the generated/safe SQL + row count). Every `/chat` and
   `/ask-data` also writes a `conversations` row. **No raw PII in either.**

9. **The single intentional PII store.** `leads.contact` is the ONE place PII is intentionally stored,
   and only when the visitor **opts in** via the hand-off. Marked sensitive / encrypt-at-rest in
   `supabase_setup.sql`; tenant-scoped by RLS. The lead's question is redacted; the audit log records the
   redacted question only — never the contact.

10. **Secrets hygiene.** All secrets via env (`.env`, gitignored); `.env.example` is a blank template;
    no hardcoded credentials.

## Known limitations / production hardening (roadmap)
- **Widget key exposure:** the embed uses a tenant key client-side. For production, proxy `/chat` through
  a Cloudflare Worker (or the client's backend) so the tenant key isn't in page source, with an Origin
  allow-list. (Phase 2.)
- Rate limiting is in-memory (single instance) — move to Redis for multi-instance.
- Connections are opened per request — add a pool for scale.
- Encrypt `leads.contact` at rest (pgcrypto / column encryption) and add retention policies.
- Presidio confidence thresholds and custom recognizers should be tuned per client's data.
