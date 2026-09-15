"""
fetch_nse_caches.py
====================
Run this ONCE from your own computer (not Render) to download NSE's
constituent lists and save them as the exact cache files
get_universe_with_sectors() in analysis_api.py looks for.

NSE blocks requests from cloud-hosting IPs (Render, AWS, etc.) with a
403 Forbidden, but does not block ordinary home/office internet
connections. Running this locally sidesteps that block entirely -
the cache files get committed to git and Render just reads them off
disk, no network call to NSE required in production.

Usage:
    cd C:\\Development\\research-api
    python fetch_nse_caches.py

This creates/overwrites these files in the current folder:
    nse_largecap_sectors.csv
    nse_midcap_sectors.csv
    nse_smallcap_sectors.csv
    nse_allequities.csv

After running, commit and push these 4 files together with
analysis_api.py so Render picks them up on the next deploy.
"""

import io
import sys

import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

NSE_LISTS = {
    "LARGECAP": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "MIDCAP": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "SMALLCAP": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

ALLEQUITIES_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


def fetch_and_save(name: str, url: str, cache_file: str, symbol_col_candidates):
    print(f"Fetching {name} from {url} ...")
    r = requests.get(url, timeout=20, headers=HEADERS)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]

    col = next((c for c in symbol_col_candidates if c in df.columns), None)
    if col is None:
        print(f"  WARNING: none of {symbol_col_candidates} found in columns "
              f"{list(df.columns)} - saving raw anyway, check manually.")
    else:
        n = len(df)
        print(f"  Got {n} rows, symbol column = '{col}'")

    df.to_csv(cache_file, index=False)
    print(f"  Saved -> {cache_file}\n")


def main():
    try:
        for cap, url in NSE_LISTS.items():
            cache_file = f"nse_{cap.lower()}_sectors.csv"
            fetch_and_save(cap, url, cache_file, ["Symbol", "SYMBOL"])

        fetch_and_save(
            "ALLEQUITIES", ALLEQUITIES_URL, "nse_allequities.csv",
            ["SYMBOL", "Symbol"],
        )
    except requests.exceptions.HTTPError as e:
        print(f"\nERROR: {e}")
        print("If this is a 403 Forbidden even from your own computer, "
              "NSE may be blocking more broadly right now - try again "
              "later, or from a different network (e.g. mobile hotspot).")
        sys.exit(1)

    print("All done. Now run:")
    print("  git add nse_largecap_sectors.csv nse_midcap_sectors.csv "
          "nse_smallcap_sectors.csv nse_allequities.csv")
    print('  git commit -m "Add cached NSE constituent lists (Render '
          'IP is blocked by NSE)"')
    print("  git push origin main")


if __name__ == "__main__":
    main()
