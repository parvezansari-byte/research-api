-- Run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Keeps a copy of every alert so the app's Notifications screen can list them.

create table if not exists push_history (
  id          bigserial primary key,
  owner_email text not null,
  title       text not null,
  body        text not null default '',
  kind        text not null default 'info',   -- signal | digest | test | info
  read        boolean not null default false,
  created_at  timestamptz not null default now()
);
create index if not exists push_history_owner_idx on push_history (owner_email, id desc);
