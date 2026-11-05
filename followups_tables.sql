-- Run once in the Supabase SQL editor. Safe to re-run.
-- One follow-up (next review date + note) per client.
create table if not exists crm_followups (
    id           text primary key,          -- 'owner@email|client_id'
    owner_email  text    not null,
    client_id    bigint  not null,
    due_date     date    not null,
    note         text    not null default '',
    updated_at   timestamptz not null default now()
);
create index if not exists crm_followups_owner_due on crm_followups (owner_email, due_date);
