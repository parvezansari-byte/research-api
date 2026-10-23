-- Run once in the Supabase SQL editor.
create table if not exists shareholding (
  symbol     text primary key,
  data       jsonb not null,
  fetched_at timestamptz not null default now()
);
