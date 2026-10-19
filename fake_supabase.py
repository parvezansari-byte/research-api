"""In-memory stand-in for the Supabase client, used only by the tests."""
from datetime import datetime, timezone


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.rows = db.tables.setdefault(name, [])
        self.filters, self.mode, self.payload = [], "select", None
        self.order_by = None
        self.max_rows = None

    def select(self, *_):
        self.mode = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def limit(self, n):
        self.max_rows = n
        return self

    def insert(self, row):
        self.mode, self.payload = "insert", row
        return self

    def update(self, fields):
        self.mode, self.payload = "update", fields
        return self

    def delete(self):
        self.mode = "delete"
        return self

    def upsert(self, row, on_conflict=None):
        self.mode, self.payload, self.conflict = "upsert", row, on_conflict
        return self

    def _match(self):
        return [r for r in self.rows if all(r.get(c) == v for c, v in self.filters)]

    def execute(self):
        if self.mode == "insert":
            row = dict(self.payload)
            for r in self.rows:  # unique indexes
                if self.name == "crm_orders" and (r["owner_email"], r["idempotency_key"]) == (
                        row["owner_email"], row["idempotency_key"]):
                    raise Exception("duplicate key value violates unique constraint")
                if self.name == "crm_manual_holdings" and all(
                        r[k] == row[k] for k in
                        ("owner_email", "client_id", "scheme_code", "folio_number")):
                    raise Exception("duplicate key value violates unique constraint")
            row["id"] = max([r["id"] for r in self.rows], default=0) + 1
            row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            self.rows.append(row)
            return FakeResult([dict(row)])
        if self.mode == "upsert":
            row, key = dict(self.payload), self.conflict
            for r in self.rows:
                if key and r.get(key) == row.get(key):
                    r.update(row)
                    return FakeResult([dict(r)])
            self.rows.append(row)
            return FakeResult([dict(row)])
        if self.mode == "update":
            hit = self._match()
            for r in hit:
                r.update(self.payload)
            return FakeResult([dict(r) for r in hit])
        if self.mode == "delete":
            hit = self._match()
            for r in hit:
                self.rows.remove(r)
            return FakeResult([dict(r) for r in hit])
        out = [dict(r) for r in self._match()]
        if self.order_by:
            col, desc = self.order_by
            out.sort(key=lambda r: (isinstance(r.get(col), str), r.get(col) if isinstance(r.get(col), (int, float)) else str(r.get(col))), reverse=desc)
        if self.max_rows is not None:
            out = out[:self.max_rows]
        return FakeResult(out)


class FakeSb:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return FakeQuery(self, name)
