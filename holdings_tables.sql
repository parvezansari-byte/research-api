-- Run once in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Safe to re-run: every statement uses IF NOT EXISTS.
--
-- Holdings the advisor records by hand for each client (fund, units, average
-- buy NAV). Separate from crm_client_holdings, which is reserved for the
-- future automatic NSE sync.

create table if not exists crm_manual_holdings (
    id            bigserial primary key,
    owner_email   text        not null,
    client_id     bigint      not null,
    scheme_code   text        not null,            -- mfapi.in scheme code
    scheme_name   text        not null,
    folio_number  text        not null default '', -- '' when not recorded
    units         numeric     not null,
    avg_nav       numeric     not null,            -- average buy NAV per unit
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- One line per fund + folio per client; adding the same fund again merges into it.
create unique index if not exists crm_manual_holdings_uq
    on crm_manual_holdings (owner_email, client_id, scheme_code, folio_number);
create index if not exists crm_manual_holdings_client_idx
    on crm_manual_holdings (owner_email, client_id);

-- As with the order tables: before real client data flows through, use the
-- service-role key in SUPABASE_KEY and enable row level security:
--   alter table crm_manual_holdings enable row level security;
