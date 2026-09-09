-- ============================================================================
-- VELA Admin Portal — Phase 0 migration (additive, idempotent)
-- ============================================================================
-- Creates the account/product/credential/usage layer for the admin portal.
-- ADDITIVE: it does NOT drop or alter the existing chat tables (tenants, chunks,
-- conversations, leads), so the running assistant keeps working. It seeds a
-- `clients` + `agents` record from the existing `demo` tenant so the new model
-- is populated. Rewiring the assistant to be agent-scoped is a follow-up step.
--
-- EXCLUDES invoices/billing (per scope decision — metering + caps only for now).
-- Run once in the Supabase SQL editor (or via psql with the main DATABASE_URL).
-- ============================================================================

create extension if not exists "pgcrypto";   -- gen_random_uuid()

-- ---------- A. Accounts & products ----------------------------------------
create table if not exists clients (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  slug          text unique not null,
  status        text not null default 'trial'
                  check (status in ('trial','active','paused','churned')),
  brand_accent  text default '#c9a25a',
  website       text,
  contact_name  text, contact_email text, contact_phone text,
  notes         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table if not exists plans (
  id                 uuid primary key default gen_random_uuid(),
  name               text not null,
  applies_to         text not null check (applies_to in ('chat','voice')),
  price_monthly_usd  numeric(10,2) not null,
  incl_chat_requests integer,
  incl_voice_minutes integer,
  overage_per_request_usd numeric(10,4),
  overage_per_minute_usd  numeric(10,4),
  hard_cap           boolean not null default true,
  active             boolean not null default true
);

create table if not exists credentials (
  id              uuid primary key default gen_random_uuid(),
  owner_type      text not null check (owner_type in ('vela','client')),
  client_id       uuid references clients(id) on delete cascade,
  provider        text not null check (provider in ('gemini','openai','vapi','twilio')),
  label           text,
  vault_secret_id uuid,          -- Supabase Vault reference (preferred)
  ciphertext      bytea,         -- or KMS-envelope ciphertext
  key_fingerprint text,          -- sha256 of the key (display/rotation only, NOT the key)
  status          text not null default 'active' check (status in ('active','rotating','revoked')),
  created_at      timestamptz not null default now()
);

create table if not exists agents (
  id            uuid primary key default gen_random_uuid(),
  client_id     uuid not null references clients(id) on delete cascade,
  type          text not null check (type in ('chat','voice')),
  name          text not null,
  status        text not null default 'draft'
                  check (status in ('draft','active','paused','disabled')),
  plan_id       uuid references plans(id),
  byok_credential_id uuid references credentials(id),   -- NULL => VELA pooled key
  llm_model     text,
  config        jsonb not null default '{}',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (client_id, type, name)
);

-- ---------- B. Auth (split read vs write; closes audit H1 at the model level) ----
create table if not exists widget_tokens (
  id              uuid primary key default gen_random_uuid(),
  agent_id        uuid not null references agents(id) on delete cascade,
  public_token    text unique not null,
  allowed_origins text[] not null default '{}',
  status          text not null default 'active' check (status in ('active','revoked')),
  created_at      timestamptz not null default now()
);

create table if not exists api_keys (
  id           uuid primary key default gen_random_uuid(),
  agent_id     uuid not null references agents(id) on delete cascade,
  key_hash     text not null,                 -- sha256(high-entropy random key)
  scope        text not null default 'ingest' check (scope in ('admin','ingest')),
  label        text,
  status       text not null default 'active' check (status in ('active','revoked')),
  last_used_at timestamptz,
  created_at   timestamptz not null default now()
);
create index if not exists api_keys_hash_idx on api_keys (key_hash);

-- ---------- C. Product data (chat knowledge + voice logs) ------------------
create table if not exists knowledge_sources (
  id          uuid primary key default gen_random_uuid(),
  agent_id    uuid not null references agents(id) on delete cascade,
  kind        text not null check (kind in ('url','text','file')),
  source      text not null,
  status      text not null default 'pending' check (status in ('pending','indexed','error')),
  chunk_count integer default 0,
  pii_types   text[] default '{}',
  indexed_at  timestamptz,
  created_at  timestamptz not null default now()
);

create table if not exists call_logs (
  id                  uuid primary key default gen_random_uuid(),
  agent_id            uuid not null references agents(id) on delete cascade,
  vapi_call_id        text,
  started_at          timestamptz,
  duration_seconds    integer,
  outcome             text,
  transcript_redacted text,
  recording_url       text,
  cost_usd            numeric(10,4)
);

-- ---------- D. Metering (source of truth + rollup; NO invoices) ------------
create table if not exists usage_events (
  id       bigint generated always as identity primary key,
  agent_id uuid not null references agents(id) on delete cascade,
  ts       timestamptz not null default now(),
  kind     text not null check (kind in ('chat_request','voice_minute','embed_tokens','llm_tokens')),
  quantity numeric(12,4) not null,
  cost_usd numeric(10,6) default 0,
  meta     jsonb default '{}'
);
create index if not exists usage_events_agent_ts_idx on usage_events (agent_id, ts);

create table if not exists usage_counters (
  agent_id      uuid not null references agents(id) on delete cascade,
  period        date not null,
  chat_requests integer not null default 0,
  voice_minutes numeric(10,2) not null default 0,
  llm_tokens    bigint not null default 0,
  cost_usd      numeric(10,4) not null default 0,
  updated_at    timestamptz not null default now(),
  primary key (agent_id, period)
);

-- ---------- E. Portal ops --------------------------------------------------
create table if not exists admin_users (
  id    uuid primary key default gen_random_uuid(),
  email text unique not null,
  role  text not null default 'staff' check (role in ('owner','staff','readonly')),
  created_at timestamptz not null default now()
);

create table if not exists admin_audit_log (
  id          bigint generated always as identity primary key,
  admin_email text,
  action      text not null,
  target_type text, target_id uuid, diff jsonb,
  ts          timestamptz not null default now()
);

-- ---------- F. Product catalogue seed (NO demo clients) --------------------
-- The portal starts with NO clients/agents — real ones are created via the admin UI.
-- We seed only VELA's own catalogue: a starter plan + the pooled Gemini credential.
insert into plans (name, applies_to, price_monthly_usd, incl_chat_requests, hard_cap)
  values ('Chat Starter', 'chat', 49.00, 2000, true)
  on conflict do nothing;

insert into credentials (owner_type, provider, label, status)
  select 'vela', 'gemini', 'VELA pooled Gemini (set vault_secret_id)', 'active'
  where not exists (select 1 from credentials where owner_type='vela' and provider='gemini');

-- Done. Follow-ups (Phase 0b): add agent_id to chunks/conversations/leads, then rewire
-- the FastAPI app to resolve config/keys per agent.
