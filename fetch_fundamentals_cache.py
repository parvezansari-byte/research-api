"""
fetch_fundamentals_cache.py
============================
Run this from your own computer (not Render) to build a local cache of
stock fundamentals (PE, PB, ROE, etc.) for the full NSE universe.

WHY THIS EXISTS
---------------
Yahoo Finance blocks the .info endpoint (which fundamentals come from)
from Render's IP with a near-empty response - confirmed via logs showing
only 1 of ~40 expected fields coming back. This isn't a transient rate
limit; it's a standing block on that IP range, and curl_cffi's browser
fingerprint impersonation didn't get past it either. Your home/office
network isn't blocked, so this script fetches everything once from here
and saves it as a JSON file that Render just reads off disk - same
pattern as the NSE stock-list cache fix.

USAGE
-----
    cd C:\\Development\\research-api
    python fetch_fundamentals_cache.py

    # Resume a previous interrupted run (skips symbols already cached):
    python fetch_fundamentals_cache.py

    # Force re-fetch everything, including symbols already cached:
    python fetch_fundamentals_cache.py --force

    # Test on a small slice first:
    python fetch_fundamentals_cache.py --limit 20

This reads the symbol list from nse_allequities.csv (already in this
repo from the earlier stock-list fix) and writes/updates
fundamentals_cache.json, saving progress every 25 stocks so an
interrupted run loses at most a couple minutes of work.

At roughly 1-2 seconds per stock (to stay polite to Yahoo and avoid
tripping a rate limit on your own network), the full ~2,568-stock
universe takes on the order of 1-1.5 hours. That's fine - see
run_weekly_refresh.bat for running this unattended on a schedule.
"""

import argparse
import io
import json
import os
import random
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis_api import get_fundamentals  # noqa: E402

ALLEQUITIES_CSV = "nse_allequities.csv"
CACHE_FILE = "fundamentals_cache.json"
SAVE_EVERY = 25
DELAY_RANGE = (1.0, 2.0)  # seconds between requests, randomised


def load_symbols() -> list[str]:
    if not os.path.exists(ALLEQUITIES_CSV):
        print(f"ERROR: {ALLEQUITIES_CSV} not found. Run fetch_nse_caches.py "
              "first to download the stock universe.")
        sys.exit(1)
    df = pd.read_csv(ALLEQUITIES_CSV)
    df.columns = [c.strip() for c in df.columns]
    col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
    symbols = df[col].astype(str).str.strip().tolist()
    return [f"{s}.NS" for s in symbols]


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_FILE)  # atomic-ish, avoids a half-written file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                         help="Re-fetch symbols already in the cache")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process the first N symbols (for testing)")
    args = parser.parse_args()

    symbols = load_symbols()
    if args.limit:
        symbols = symbols[:args.limit]

    cache = load_cache()
    print(f"Loaded {len(symbols)} symbols, {len(cache)} already cached.")

    todo = symbols if args.force else [s for s in symbols if s not in cache]
    print(f"Fetching {len(todo)} symbols "
          f"({'forced re-fetch of all' if args.force else 'skipping already-cached'})...\n")

    ok, failed = 0, 0
    start = time.time()

    for i, sym in enumerate(todo, 1):
        try:
            data = get_fundamentals(sym)
            if data.get("_fetch_failed"):
                print(f"  [{i}/{len(todo)}] {sym}: still rate-limited, skipping "
                      f"(will retry next run)")
                failed += 1
            else:
                data["_cached_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                cache[sym] = data
                ok += 1
                if i % 10 == 0 or i == len(todo):
                    elapsed = time.time() - start
                    rate = i / elapsed if elapsed > 0 else 0
                    remaining = (len(todo) - i) / rate if rate > 0 else 0
                    print(f"  [{i}/{len(todo)}] {sym}: OK "
                          f"(ok={ok} failed={failed}, "
                          f"~{remaining/60:.0f} min remaining)")
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {sym}: ERROR {e}")
            failed += 1

        if i % SAVE_EVERY == 0:
            save_cache(cache)

        time.sleep(random.uniform(*DELAY_RANGE))

    save_cache(cache)
    print(f"\nDone. {ok} fetched OK, {failed} failed/skipped this run, "
          f"{len(cache)} total symbols now cached.")
    print(f"Saved -> {CACHE_FILE}")
    if failed > 0:
        print(f"\n{failed} symbols failed (likely still rate-limited even "
              f"from your network, or genuinely delisted/invalid symbols). "
              f"Re-run this script later without --force to retry just those.")
    print("\nNow run:")
    print(f"  git add {CACHE_FILE}")
    print('  git commit -m "Update fundamentals cache"')
    print("  git push origin main")


if __name__ == "__main__":
    main()
