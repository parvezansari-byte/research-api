"""
normalize_fund_names.py (v2 - corrected)
==========================================
v1 had a bad garbage-detection heuristic (flagged any name missing the
literal substring "fund"/"etf"/"scheme") which wrongly caught every
Fund-of-Funds product, since those are abbreviated "FoF"/"FOF" and never
spell out "fund" - plus other legitimate products like "SBI Gold-Reg(G)",
"LIC MF ULIS", ELSS "Tax Saver" funds, etc. This version instead matches
the disclaimer text directly, which is far safer.

USAGE
-----
    cd C:\\Development\\research-api

    # Preview only - shows what would change, touches nothing:
    python normalize_fund_names.py --dry-run

    # Apply for real (writes funds_data.json, keeps a .bak backup):
    python normalize_fund_names.py
"""

import argparse
import json
import re
import shutil

FILE = "funds_data.json"

# Exact phrases that only appear in the corrupted disclaimer-paragraph
# rows - never in a real fund/ETF/scheme name.
DISCLAIMER_MARKERS = [
    "disclaimer",
    "necessary precautions",
    "no representations",
    "warranties are made",
    "accuracy or completeness",
    "held liable for the contents",
    "express or implied",
    "lapse or insufficiency",
]


def is_garbage_row(name: str) -> bool:
    lname = name.lower()
    return any(marker in lname for marker in DISCLAIMER_MARKERS)


def normalize_name(name: str) -> str:
    m = re.search(r"-Reg\(G\)\s*$", name)
    if m:
        return name[: m.start()] + " - Regular Plan - Growth"

    m = re.search(r"-Reg\(IDCW\)\s*$", name)
    if m:
        return name[: m.start()] + " - Regular Plan - IDCW"

    m = re.search(r"(?<!Reg)(?<!Dir)\(G\)\s*$", name)
    if m:
        return name[: m.start()] + " - Regular Plan - Growth"

    m = re.search(r"(?<!Reg)(?<!Dir)\(IDCW\)\s*$", name)
    if m:
        return name[: m.start()] + " - Regular Plan - IDCW"

    return name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Loaded {len(data)} rows from {FILE}\n")

    kept = []
    removed = []
    renamed = []
    unchanged_count = 0

    for row in data:
        name = row.get("name") or ""
        if is_garbage_row(name):
            removed.append(name)
            continue

        new_name = normalize_name(name)
        if new_name != name:
            renamed.append((name, new_name))
            row["name"] = new_name
        else:
            unchanged_count += 1
        kept.append(row)

    print(f"=== Would remove {len(removed)} garbage/disclaimer rows (FULL list) ===")
    for r in removed:
        print("  -", repr(r))

    print(f"\n=== Would rename {len(renamed)} fund names (FULL list) ===")
    for old, new in renamed:
        print(f"  {old!r}")
        print(f"    -> {new!r}")

    print(f"\n=== {unchanged_count} rows left as-is (ETFs, edge cases) - sample: ===")
    unchanged_sample = [r.get("name") for r in kept
                         if normalize_name(r.get("name") or "") == (r.get("name") or "")][:30]
    for n in unchanged_sample:
        print("  -", repr(n))

    print(f"\nFinal count would be: {len(kept)} funds "
          f"(was {len(data)}, removed {len(removed)})")

    if args.dry_run:
        print("\n--dry-run: no files were changed. "
              "Re-run without --dry-run to apply.")
        return

    shutil.copy(FILE, FILE + ".bak")
    print(f"\nBacked up original to {FILE}.bak")

    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=1)
    print(f"Wrote {len(kept)} funds -> {FILE}")

    print("\nNow run:")
    print(f"  git add {FILE}")
    print('  git commit -m "Normalize fund names to AMFI format, remove '
          'corrupted disclaimer rows"')
    print("  git push origin main")


if __name__ == "__main__":
    main()
