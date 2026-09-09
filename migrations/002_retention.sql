-- 002_retention.sql — enforce the retention schedule published at
-- byvela.online/privacy: chat conversations ≤ 90 days, chat hand-off leads
-- ≤ 180 days. Runs nightly inside Postgres via pg_cron (Supabase ships it;
-- it just has to be enabled). Idempotent: re-running replaces the jobs.
--
-- Apply (as the postgres role, e.g. Supabase SQL editor or psql on DATABASE_URL):
--   \i migrations/002_retention.sql
-- Verify:  select jobname, schedule, command from cron.job;
-- Undo:    select cron.unschedule('vela-purge-conversations');
--          select cron.unschedule('vela-purge-leads');

create extension if not exists pg_cron;

-- pg_cron runs jobs as the scheduling role (postgres), which bypasses RLS —
-- that is intended here: the purge is tenant-agnostic by design.
select cron.unschedule(jobid) from cron.job where jobname in ('vela-purge-conversations', 'vela-purge-leads');

select cron.schedule(
  'vela-purge-conversations',
  '15 3 * * *',                                   -- 03:15 UTC daily
  $$delete from conversations where ts < now() - interval '90 days'$$
);

select cron.schedule(
  'vela-purge-leads',
  '20 3 * * *',                                   -- 03:20 UTC daily
  $$delete from leads where ts < now() - interval '180 days'$$
);
