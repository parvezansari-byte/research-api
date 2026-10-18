-- Run this once in Supabase: SQL Editor -> New query -> paste -> Run.
-- (Same way you ran crm_schema.sql.)

create table if not exists push_tokens (
  token        text primary key,          -- the phone's FCM device token
  owner_email  text not null,
  platform     text default 'android',
  updated_at   timestamptz default now()
);
create index if not exists push_tokens_owner_idx on push_tokens (owner_email);

-- Remembers the last signal / digest sent so repeated checks don't re-alert.
create table if not exists push_state (
  key         text primary key,
  value       text,
  updated_at  timestamptz default now()
);
