-- =====================================================================
-- VELA AI Business-Assistant — Supabase setup (paste into the SQL editor)
-- =====================================================================
-- Run this ONCE in your Supabase project's SQL editor. It:
--   1. enables the pgvector extension
--   2. creates the tables: tenants, chunks (vectors), conversations, leads
--   3. adds indexes (ivfflat vector index + tenant_id indexes)
--   4. turns on Row-Level Security so a tenant can never read another tenant's rows
--   5. creates a READ-ONLY role for the secured Chat-to-SQL feature
--   6. loads a small Chinook sample dataset as the per-tenant "live data" source
--
-- Tenant isolation model:
--   The app opens a DB session and runs  set_config('app.tenant_id', '<tenant>', false)
--   before touching tenant data. Every RLS policy below compares each row against
--   current_setting('app.tenant_id', true). A session scoped to tenant A therefore
--   cannot SELECT/INSERT tenant B's chunks, conversations, or leads.
-- =====================================================================

-- 1. Extensions ------------------------------------------------------------
create extension if not exists vector;

-- 2a. tenants --------------------------------------------------------------
-- API keys are stored ONLY as a sha256 hash (never plaintext). The app seeds
-- these on startup from the TENANT_KEYS env (bootstrap), or you insert them here.
create table if not exists tenants (
  id            text primary key,
  name          text not null,
  api_key_hash  text not null,               -- sha256(hex) of the tenant's API key
  accent        text default '#c8a24a',
  created_at    timestamptz not null default now()
);

-- 2b. chunks (pgvector store) ---------------------------------------------
-- content is ALREADY PII-redacted before insert. embedding is 768-dim (Gemini
-- text-embedding-004). Change vector(768) if you swap the embedding model + EMBED_DIM.
create table if not exists chunks (
  id          bigint generated always as identity primary key,
  tenant_id   text not null references tenants(id) on delete cascade,
  content     text not null,
  embedding   vector(768) not null,
  source      text,
  created_at  timestamptz not null default now()
);

-- 2c. conversations (audit of every /chat turn) ---------------------------
-- NO raw PII. question_redacted + pii_types (the TYPES found, not the values).
create table if not exists conversations (
  id                 bigint generated always as identity primary key,
  tenant_id          text not null references tenants(id) on delete cascade,
  ts                 timestamptz not null default now(),
  question_redacted  text,
  answer             text,
  grounded           boolean,
  sources            jsonb,
  pii_types          jsonb,
  injection_flag     boolean
);

-- 2d. leads (the ONE intentional PII store) -------------------------------
-- `contact` is sensitive PII, captured only when the visitor OPTS IN via the
-- "I don't know" hand-off. Treat as encrypt-at-rest / sensitive. Tenant-scoped.
create table if not exists leads (
  id                 bigint generated always as identity primary key,
  tenant_id          text not null references tenants(id) on delete cascade,
  ts                 timestamptz not null default now(),
  question_redacted  text,
  contact            text                     -- SENSITIVE PII (opt-in). Encrypt at rest.
);

-- 3. Indexes ---------------------------------------------------------------
create index if not exists chunks_tenant_idx        on chunks (tenant_id);
create index if not exists conversations_tenant_idx on conversations (tenant_id);
create index if not exists leads_tenant_idx         on leads (tenant_id);

-- Approximate-NN index for cosine similarity. ivfflat needs ANALYZE + data to be
-- effective; for tiny demos a seq scan is fine. Rebuild `lists` as data grows.
create index if not exists chunks_embedding_idx
  on chunks using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- 4. Row-Level Security ----------------------------------------------------
alter table chunks        enable row level security;
alter table conversations enable row level security;
alter table leads         enable row level security;

-- FORCE RLS so even the table owner is subject to the policies (Supabase's
-- service role / owner would otherwise bypass RLS).
alter table chunks        force row level security;
alter table conversations force row level security;
alter table leads         force row level security;

-- Policy: a row is visible/writable only when its tenant_id equals the session's
-- app.tenant_id GUC. `true` as the 2nd arg => returns '' (not error) when unset,
-- so an unscoped session matches nothing.
drop policy if exists tenant_isolation on chunks;
create policy tenant_isolation on chunks
  using (tenant_id = current_setting('app.tenant_id', true))
  with check (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists tenant_isolation on conversations;
create policy tenant_isolation on conversations
  using (tenant_id = current_setting('app.tenant_id', true))
  with check (tenant_id = current_setting('app.tenant_id', true));

drop policy if exists tenant_isolation on leads;
create policy tenant_isolation on leads
  using (tenant_id = current_setting('app.tenant_id', true))
  with check (tenant_id = current_setting('app.tenant_id', true));

-- NOTE: the app connects with a role that is subject to RLS. If you connect as a
-- BYPASSRLS superuser you will NOT see isolation — use a normal role (Supabase's
-- default 'authenticated'/service connection string works; force RLS above covers owner).

-- =====================================================================
-- 5. Chinook sample + READ-ONLY role (for secured Chat-to-SQL)
-- =====================================================================
-- A compact snake_case Chinook subset. This data is shared "live data" (NOT tenant
-- RLS-scoped) — it represents a business's operational DB that Chat-to-SQL queries.

create table if not exists artists (
  artist_id integer primary key,
  name      text
);
create table if not exists albums (
  album_id  integer primary key,
  title     text,
  artist_id integer references artists(artist_id)
);
create table if not exists genres (
  genre_id integer primary key,
  name     text
);
create table if not exists tracks (
  track_id     integer primary key,
  name         text,
  album_id     integer references albums(album_id),
  genre_id     integer references genres(genre_id),
  unit_price   numeric(10,2),
  milliseconds integer
);
create table if not exists customers (
  customer_id integer primary key,
  first_name  text,
  last_name   text,
  country     text,
  email       text
);
create table if not exists invoices (
  invoice_id      integer primary key,
  customer_id     integer references customers(customer_id),
  invoice_date    date,
  total           numeric(10,2),
  billing_country text
);
create table if not exists invoice_items (
  invoice_line_id integer primary key,
  invoice_id      integer references invoices(invoice_id),
  track_id        integer references tracks(track_id),
  unit_price      numeric(10,2),
  quantity        integer
);

-- --- seed data (idempotent) ---
insert into artists (artist_id, name) values
  (1,'AC/DC'),(2,'Aerosmith'),(3,'Miles Davis'),(4,'Daft Punk')
on conflict do nothing;

insert into genres (genre_id, name) values
  (1,'Rock'),(2,'Jazz'),(3,'Electronic')
on conflict do nothing;

insert into albums (album_id, title, artist_id) values
  (1,'Back in Black',1),(2,'Toys in the Attic',2),
  (3,'Kind of Blue',3),(4,'Discovery',4)
on conflict do nothing;

insert into tracks (track_id, name, album_id, genre_id, unit_price, milliseconds) values
  (1,'Hells Bells',1,1,0.99,312000),
  (2,'Back in Black',1,1,0.99,255000),
  (3,'Walk This Way',2,1,0.99,208000),
  (4,'So What',3,2,0.99,562000),
  (5,'One More Time',4,3,0.99,320000),
  (6,'Harder Better Faster Stronger',4,3,0.99,224000)
on conflict do nothing;

insert into customers (customer_id, first_name, last_name, country, email) values
  (1,'Alice','Ng','USA','alice@example.com'),
  (2,'Bruno','Costa','Brazil','bruno@example.com'),
  (3,'Chika',' Obi','Nigeria','chika@example.com'),
  (4,'Dara','Khan','UAE','dara@example.com')
on conflict do nothing;

insert into invoices (invoice_id, customer_id, invoice_date, total, billing_country) values
  (1,1,'2025-02-11',9.90,'USA'),
  (2,1,'2025-06-03',4.95,'USA'),
  (3,2,'2025-03-22',2.97,'Brazil'),
  (4,3,'2025-07-15',19.80,'Nigeria'),
  (5,4,'2025-08-01',0.99,'UAE')
on conflict do nothing;

insert into invoice_items (invoice_line_id, invoice_id, track_id, unit_price, quantity) values
  (1,1,1,0.99,10),
  (2,2,2,0.99,5),
  (3,3,3,0.99,3),
  (4,4,4,0.99,20),
  (5,5,5,0.99,1)
on conflict do nothing;

-- --- READ-ONLY role for Chat-to-SQL ---------------------------------------
-- The app's READONLY_DATABASE_URL should connect as this role. It can ONLY SELECT
-- the Chinook tables. Even if the SQL safety gate were bypassed, a write fails here.
-- Replace 'CHANGE_ME_STRONG_PASSWORD' before running, and put the matching
-- connection string in READONLY_DATABASE_URL.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'vela_readonly') then
    create role vela_readonly login password 'CHANGE_ME_STRONG_PASSWORD';
  end if;
end $$;

grant usage on schema public to vela_readonly;
grant select on artists, albums, genres, tracks, customers, invoices, invoice_items
  to vela_readonly;
-- Explicitly ensure NO write privileges (default, but be explicit):
revoke insert, update, delete, truncate on all tables in schema public from vela_readonly;
-- Do NOT grant select on tenants/chunks/conversations/leads to the read-only role.

-- Done. Verify:  select * from tenants;   select count(*) from tracks;
