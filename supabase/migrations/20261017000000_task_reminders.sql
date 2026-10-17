-- TaskMate AI: persistent deadline reminders.
-- Additive only: creates two tables and does not modify or delete task rows.
-- Run once in the Supabase SQL editor (or with `supabase db push`).

create extension if not exists pgcrypto;

-- One row per workspace. TaskMate has no user accounts yet, so the only row
-- is id = 'default'. See README "Current limitations".
create table if not exists public.reminder_preferences (
    id text primary key default 'default' check (id = 'default'),
    timezone text not null default 'UTC',
    in_app_enabled boolean not null default true,
    before_deadline_hours integer not null default 24
        check (before_deadline_hours between 1 and 168),
    deadline_day_hour integer not null default 9
        check (deadline_day_hour between 0 and 23),
    overdue_interval_hours integer not null default 24
        check (overdue_interval_hours between 1 and 168),
    max_overdue_reminders integer not null default 3
        check (max_overdue_reminders between 0 and 30),
    whatsapp_enabled boolean not null default false,
    whatsapp_number text check (whatsapp_number is null or whatsapp_number ~ '^\+[1-9][0-9]{7,14}$'),
    whatsapp_consent_at timestamptz,
    updated_at timestamptz not null default now(),
    -- Opting in to WhatsApp requires a number and a recorded consent time.
    check (not whatsapp_enabled or (whatsapp_number is not null and whatsapp_consent_at is not null))
);

create table if not exists public.task_reminders (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.tasks (id) on delete cascade,
    kind text not null check (kind in ('before_deadline', 'deadline_day', 'overdue')),
    sequence integer not null default 0 check (sequence >= 0),
    channel text not null default 'in_app' check (channel in ('in_app', 'whatsapp')),
    recipient text,
    scheduled_for timestamptz not null,
    due_at_snapshot timestamptz not null,
    message text not null,
    status text not null default 'pending'
        check (status in ('pending', 'sending', 'sent', 'failed', 'skipped', 'cancelled', 'superseded')),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    next_attempt_at timestamptz,
    last_error text,
    provider_message_id text,
    sent_at timestamptz,
    read_at timestamptz,
    dismissed_at timestamptz,
    snoozed_until timestamptz,
    response text check (response in ('completed', 'not_yet', 'snoozed', 'deadline_changed')),
    responded_at timestamptz,
    -- task:kind:sequence:deadline-epoch:channel. Makes every sync idempotent
    -- across restarts and concurrent workers.
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists task_reminders_task_id_idx on public.task_reminders (task_id);
create index if not exists task_reminders_inbox_idx
    on public.task_reminders (channel, status, scheduled_for desc)
    where dismissed_at is null;
create index if not exists task_reminders_delivery_idx
    on public.task_reminders (channel, status, next_attempt_at)
    where channel <> 'in_app';

-- Row Level Security: no policies are defined, so the anon and authenticated
-- roles cannot read or write these tables from a browser. The backend uses a
-- server-side secret key, which bypasses RLS. Never expose that key to the
-- frontend.
alter table public.reminder_preferences enable row level security;
alter table public.task_reminders enable row level security;
