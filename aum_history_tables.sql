-- Run once in the Supabase SQL editor. Safe to re-run.
-- One row per owner per day with the whole client book's value.
create table if not exists crm_aum_history (
    id           text primary key,          -- 'owner@email|YYYY-MM-DD'
    owner_email  text    not null,
    day          date    not null,
    value        numeric not null,          -- current value of all priced holdings
    invested     numeric not null,          -- amount invested in those holdings
    clients      int     not null default 0,
    holdings     int     not null default 0,
    unpriced     int     not null default 0,
    created_at   timestamptz not null default now()
);
create index if not exists crm_aum_history_owner_day on crm_aum_history (owner_email, day desc);
