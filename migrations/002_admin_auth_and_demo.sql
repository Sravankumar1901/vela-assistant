-- ============================================================================
-- VELA Admin Portal — 002: admin login + realistic demo data (idempotent)
-- ============================================================================
-- Adds password auth to admin_users and seeds a full, believable demo account
-- set (clients + chat/voice agents + plans + usage + events + conversations +
-- call logs + credentials + audit trail) so the admin dashboard and tables look
-- alive. Safe to run repeatedly: every insert is guarded (on conflict / where
-- not exists) and every counter uses upsert.
--
-- Demo login (report to the operator):  admin@vela.local / VelaAdmin!2026
--   password_hash = sha256(password) hex — the SAME scheme as security.hash_key,
--   so verify_admin() can compare hash_key(password) to password_hash directly.
--
-- Run: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_admin_auth_and_demo.sql
-- ============================================================================

-- ---------- A. Admin login -------------------------------------------------
alter table admin_users add column if not exists password_hash text;

-- Seed the OWNER login. pgcrypto (digest) comes from migration 001's extension.
-- On re-run, refresh the hash + role so the credential is deterministic.
insert into admin_users (email, role, password_hash)
values ('admin@vela.local', 'owner', encode(digest('VelaAdmin!2026', 'sha256'), 'hex'))
on conflict (email) do update
  set password_hash = excluded.password_hash,
      role = 'owner';

-- ---------- B. Voice plan (Vera Pro) --------------------------------------
-- plans has no unique key on name, so guard with WHERE NOT EXISTS (idempotent).
insert into plans (name, applies_to, price_monthly_usd, incl_voice_minutes, hard_cap)
select 'Vera Pro', 'voice', 143.00, 500, true
where not exists (select 1 from plans where name = 'Vera Pro');

-- ---------- (demo client/agent/usage/event seed REMOVED) ----------------
-- The portal starts EMPTY — real clients are created via the admin UI.
-- (Earlier revisions seeded Aura/Lama/Meridian demo data here; removed so a
--  fresh setup has no dummy clients.) Keeps: admin login (A) + Vera Pro plan (B).
