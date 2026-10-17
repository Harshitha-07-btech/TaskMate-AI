-- TaskMate AI: let the backend's server-side key use the reminder tables.
-- Additive only: grants privileges, does not touch any rows.
-- Run once in the Supabase SQL editor after 20261017000000_task_reminders.sql.
--
-- This project does not auto-grant new public tables to the Data API roles, so
-- the backend's secret key (service_role) got "42501 permission denied" on both
-- tables. service_role bypasses RLS but still needs ordinary table privileges.
--
-- Only service_role is granted, and only what backend/reminders.py uses:
-- select, plus insert/update for upserts. No delete: reminder rows are removed
-- by the ON DELETE CASCADE from tasks. anon and authenticated get nothing, and
-- RLS stays enabled with no policies, so browsers still can't reach these tables.

grant usage on schema public to service_role;
grant select, insert, update on table public.reminder_preferences to service_role;
grant select, insert, update on table public.task_reminders to service_role;

-- Make PostgREST pick up the new privileges immediately.
notify pgrst, 'reload schema';
