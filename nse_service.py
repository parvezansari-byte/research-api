"""
nse_service.py - the one place that talks to NSE for mutual fund orders.

TWO IMPLEMENTATIONS behind the same three methods (submit / status / cancel):

  StubNse  - a safe simulator. Nothing leaves this server; no money moves.
             Orders step through realistic statuses over ~20 seconds so the
             app's screens can be built and tested end to end.
  LiveNse  - placeholder for the real NSE NMF II integration. It is NOT
             wired up: it needs NSE's API specification (endpoints, request
             signing, UAT base URL, IP whitelist rules). Until then it
             refuses to run rather than guess.

Which one is used is decided by the NSE_MODE environment variable:
  NSE_MODE=stub  (default)  -> StubNse
  NSE_MODE=live             -> LiveNse (will refuse until implemented)

Credentials (NSE member id / key / secret) must only ever live in Render
environment variables - never in code, the app, or git. LiveNse will read
them from the environment when it is written; the stub never touches them.

Order dicts passed in here are rows from the `crm_orders` table.
"""
import hashlib
import os
import re
from datetime import datetime, timezone

# Statuses an order moves through. The last three are final.
STATUS_CREATED = "CREATED"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_PAYMENT_PENDING = "PAYMENT_PENDING"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_ALLOTTED = "ALLOTTED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
TERMINAL_STATUSES = {STATUS_ALLOTTED, STATUS_FAILED, STATUS_CANCELLED}

# In the stub, an order for exactly this amount is rejected, so the failure
# path in the app can be tested on demand.
STUB_REJECT_AMOUNT = 111


class NseNotReady(Exception):
    """Live NSE ordering is not implemented / not configured yet."""


def parse_ts(value) -> datetime:
    """Parse a Postgres/Supabase timestamp robustly (any fraction length)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00").replace(" ", "T")
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:
        head, frac, tail = m.groups()
        frac = ((frac or ".0")[1:] + "000000")[:6]
        s = f"{head}.{frac}{tail}"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StubNse:
    name = "STUB"
    ready = True

    def submit(self, order: dict) -> dict:
        if float(order.get("amount") or 0) == STUB_REJECT_AMOUNT:
            return {
                "nse_order_ref": None,
                "status": STATUS_FAILED,
                "reason": "Simulated rejection (test amount 111)",
            }
        digest = hashlib.sha1(str(order["idempotency_key"]).encode()).hexdigest()
        return {
            "nse_order_ref": "STUB-" + digest[:10].upper(),
            "status": STATUS_SUBMITTED,
            "reason": None,
        }

    def status(self, order: dict) -> dict:
        """Status by age of the order, so it advances on its own."""
        current = order.get("status")
        if current in TERMINAL_STATUSES:
            return {"status": current, "reason": order.get("failure_reason")}
        age = (datetime.now(timezone.utc) - parse_ts(order["created_at"])).total_seconds()
        if age < 8:
            status = STATUS_PAYMENT_PENDING
        elif age < 20:
            status = STATUS_CONFIRMED
        else:
            status = STATUS_ALLOTTED
        return {"status": status, "reason": None}

    def cancel(self, order: dict) -> dict:
        return {"status": STATUS_CANCELLED, "reason": "Cancelled by user"}


class LiveNse:
    name = "LIVE"
    ready = False  # flip to True only once the real integration exists

    def _refuse(self):
        raise NseNotReady(
            "Live NSE ordering isn't wired up yet - it needs NSE's API "
            "specification, the UAT base URL and the IP whitelist rules. "
            "Set NSE_MODE=stub (or leave it unset) to use the simulator."
        )

    def submit(self, order: dict) -> dict:
        self._refuse()

    def status(self, order: dict) -> dict:
        self._refuse()

    def cancel(self, order: dict) -> dict:
        self._refuse()


def get_nse_client():
    mode = os.environ.get("NSE_MODE", "stub").strip().lower()
    if mode == "live":
        return LiveNse()
    return StubNse()
