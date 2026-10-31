"""
match_report.py - run on your PC to see why funds don't match AMFI.

  python match_report.py            (uses the NAVAll.txt you downloaded to %TEMP%\\nav.txt)
  python match_report.py C:\\path\\to\\nav.txt

Prints a summary and writes match_report.txt: for each unmatched fund, the closest
AMFI Regular-Growth scheme and a similarity score. Nothing is sent anywhere.
"""
import difflib
import json
import os
import sys
import types

# This script only reads files, so it doesn't need the web packages nav_service imports.
for _mod, _attrs in (("fastapi", {"HTTPException": Exception}), ("requests", {})):
    try:
        __import__(_mod)
    except ImportError:
        _stub = types.ModuleType(_mod)
        for _k, _v in _attrs.items():
            setattr(_stub, _k, _v)
        sys.modules[_mod] = _stub

import nav_service as ns

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("TEMP", "."), "nav.txt")
with open(path, encoding="utf-8-sig", errors="replace") as f:
    data = ns.parse_navall(f.read())
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "funds_data.json"), encoding="utf-8") as f:
    funds = json.load(f)

index = ns._build_name_index(data)
print(f"AMFI schemes read: {len(data)}")

# candidate pool: Regular + Growth schemes only (that's what the fund database lists)
pool = {}
for code, row in data.items():
    plan, opt = (row.get("plan") or "").lower(), (row.get("option") or "").lower()
    if ("regular" in plan or not plan) and ("growth" in opt or not opt) and "idcw" not in opt:
        pool[ns.norm_name(ns._full_name(row))] = ns._full_name(row)
by_first = {}
for k in pool:
    by_first.setdefault(k.split(" ")[0], []).append(k)

lines, unmatched = [], 0
for fund in funds:
    name = fund["name"]
    if index.get(ns.norm_name(name)):
        continue
    unmatched += 1
    key = ns.norm_name(name)
    cands = by_first.get(key.split(" ")[0], [])
    best = difflib.get_close_matches(key, cands, n=1, cutoff=0.0)
    if best:
        ratio = difflib.SequenceMatcher(None, key, best[0]).ratio()
        lines.append(f"{ratio:.2f} | {name}\n       -> {pool[best[0]]}")
    else:
        lines.append(f"0.00 | {name}\n       -> (no AMFI scheme starts with '{key.split(' ')[0]}')")
lines.sort(reverse=True)
with open("match_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"Funds in database: {len(funds)}   unmatched: {unmatched}")
print("Written to match_report.txt. Highest-similarity pairs (likely just spelling differences):\n")
print("\n".join(lines[:30]))
