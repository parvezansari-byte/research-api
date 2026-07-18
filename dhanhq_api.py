"""
dhanhq_api.py
=============
Low-level wrapper around the official DhanHQ v2 Python SDK.

All Dhan-specific details (security IDs, exchange segments, response
unwrapping, instrument-master lookups) live here so the rest of your
code never has to know about them.

Setup
-----
    pip install dhanhq pandas

Credentials (recommended: environment variables)
    set DHAN_CLIENT_ID=1000000001          (Windows)
    set DHAN_ACCESS_TOKEN=eyJhbGciOi...

    or pass them directly:  DhanAPI(client_id="...", access_token="...")

Tested with dhanhq SDK v2.1+ (DhanContext style). Falls back to the
older constructor automatically if an older SDK is installed.
"""

import os
import logging
from datetime import datetime, timedelta

import pandas as pd

try:
    # dhanhq >= 2.1
    from dhanhq import DhanContext, dhanhq
    _NEW_SDK = True
except ImportError:
    # dhanhq < 2.1
    from dhanhq import dhanhq
    _NEW_SDK = False

logger = logging.getLogger("dhanhq_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Instrument master (symbol -> security id mapping), published daily by Dhan
COMPACT_CSV_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
LOCAL_SCRIP_FILE = "dhan_scrip_master.csv"


class DhanAPIError(Exception):
    """Raised when a Dhan API call fails."""


class DhanAPI:
    """Thin, safe wrapper over the dhanhq SDK."""

    # ---- handy constants (mirror SDK values) ----
    NSE = "NSE_EQ"
    BSE = "BSE_EQ"
    NSE_FNO = "NSE_FNO"
    IDX = "IDX_I"

    BUY = "BUY"
    SELL = "SELL"

    CNC = "CNC"            # delivery
    INTRADAY = "INTRADAY"
    MARGIN = "MARGIN"

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "STOP_LOSS"
    SLM = "STOP_LOSS_MARKET"

    # ------------------------------------------------------------------ #
    # Init / auth
    # ------------------------------------------------------------------ #
    def __init__(self, client_id: str | None = None, access_token: str | None = None):
        client_id = client_id or os.getenv("DHAN_CLIENT_ID")
        access_token = access_token or os.getenv("DHAN_ACCESS_TOKEN")

        if not client_id or not access_token:
            raise DhanAPIError(
                "Missing credentials. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
                "environment variables, or pass them to DhanAPI(...)."
            )

        if _NEW_SDK:
            self._ctx = DhanContext(client_id, access_token)
            self.dhan = dhanhq(self._ctx)
        else:
            self.dhan = dhanhq(client_id, access_token)

        self.client_id = client_id
        self._scrip_df: pd.DataFrame | None = None
        logger.info("DhanAPI initialised for client %s", client_id)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _unwrap(resp: dict, what: str = "request"):
        """Dhan responses look like {'status': 'success', 'data': ...}."""
        if isinstance(resp, dict) and resp.get("status") == "failure":
            raise DhanAPIError(f"{what} failed: {resp.get('remarks') or resp}")
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    def _load_scrip_master(self, force: bool = False) -> pd.DataFrame:
        """Download (once) and cache Dhan's instrument master CSV."""
        if self._scrip_df is not None and not force:
            return self._scrip_df
        if os.path.exists(LOCAL_SCRIP_FILE) and not force:
            logger.info("Loading cached scrip master: %s", LOCAL_SCRIP_FILE)
            self._scrip_df = pd.read_csv(LOCAL_SCRIP_FILE, low_memory=False)
        else:
            logger.info("Downloading scrip master from Dhan (one-time, ~few MB)...")
            self._scrip_df = pd.read_csv(COMPACT_CSV_URL, low_memory=False)
            self._scrip_df.to_csv(LOCAL_SCRIP_FILE, index=False)
        return self._scrip_df

    # ------------------------------------------------------------------ #
    # Instrument lookup
    # ------------------------------------------------------------------ #
    def get_security_id(self, symbol: str, exchange: str = "NSE") -> str:
        """
        Resolve a trading symbol (e.g. 'RELIANCE', 'TCS') to Dhan's
        numeric security_id for equity.
        """
        df = self._load_scrip_master()
        symbol = symbol.strip().upper()

        # Column names in the compact CSV
        exch_col = "SEM_EXM_EXCH_ID"
        sym_col = "SEM_TRADING_SYMBOL"
        id_col = "SEM_SMST_SECURITY_ID"
        instr_col = "SEM_INSTRUMENT_NAME" if "SEM_INSTRUMENT_NAME" in df.columns else None

        mask = (df[exch_col].astype(str).str.upper() == exchange.upper()) & (
            df[sym_col].astype(str).str.upper() == symbol
        )
        if instr_col:
            mask &= df[instr_col].astype(str).str.upper().isin(["EQUITY", "ES", "EQ"])

        rows = df[mask]
        if rows.empty:
            # relax the instrument filter and retry
            mask = (df[exch_col].astype(str).str.upper() == exchange.upper()) & (
                df[sym_col].astype(str).str.upper() == symbol
            )
            rows = df[mask]
        if rows.empty:
            raise DhanAPIError(f"Symbol '{symbol}' not found on {exchange}")

        return str(rows.iloc[0][id_col])

    # ------------------------------------------------------------------ #
    # Account / portfolio
    # ------------------------------------------------------------------ #
    @staticmethod
    def _empty_ok(resp, what: str) -> list:
        """
        Unwrap list endpoints where Dhan reports an *error* when there is
        simply no data (e.g. DH-1111 'No holdings available'). Treat those
        as an empty list instead of a failure.
        """
        try:
            data = DhanAPI._unwrap(resp, what)
            return data or []
        except DhanAPIError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("no holdings", "no positions", "no orders",
                                      "no trades", "no data", "dh-1111")):
                return []
            raise

    def get_funds(self) -> dict:
        return self._unwrap(self.dhan.get_fund_limits(), "get_funds")

    def get_holdings(self) -> list:
        return self._empty_ok(self.dhan.get_holdings(), "get_holdings")

    def get_positions(self) -> list:
        return self._empty_ok(self.dhan.get_positions(), "get_positions")

    def get_trade_book(self) -> list:
        return self._empty_ok(self.dhan.get_trade_book(), "get_trade_book")

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def place_order(
        self,
        security_id: str,
        transaction_type: str,          # BUY / SELL
        quantity: int,
        order_type: str = "MARKET",     # MARKET / LIMIT / STOP_LOSS / STOP_LOSS_MARKET
        product_type: str = "CNC",      # CNC / INTRADAY / MARGIN
        price: float = 0,
        trigger_price: float = 0,
        exchange_segment: str = "NSE_EQ",
        validity: str = "DAY",
        tag: str | None = None,
    ) -> dict:
        resp = self.dhan.place_order(
            security_id=str(security_id),
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            quantity=int(quantity),
            order_type=order_type,
            product_type=product_type,
            price=float(price),
            trigger_price=float(trigger_price),
            validity=validity,
            tag=tag,
        )
        return self._unwrap(resp, "place_order")

    def modify_order(
        self,
        order_id: str,
        order_type: str,
        quantity: int,
        price: float,
        trigger_price: float = 0,
        leg_name: str = "ENTRY_LEG",
        disclosed_quantity: int = 0,
        validity: str = "DAY",
    ) -> dict:
        resp = self.dhan.modify_order(
            order_id=order_id,
            order_type=order_type,
            leg_name=leg_name,
            quantity=int(quantity),
            price=float(price),
            trigger_price=float(trigger_price),
            disclosed_quantity=disclosed_quantity,
            validity=validity,
        )
        return self._unwrap(resp, "modify_order")

    def cancel_order(self, order_id: str) -> dict:
        return self._unwrap(self.dhan.cancel_order(order_id), "cancel_order")

    def get_orders(self) -> list:
        return self._empty_ok(self.dhan.get_order_list(), "get_orders")

    def get_order_status(self, order_id: str) -> dict:
        return self._unwrap(self.dhan.get_order_by_id(order_id), "get_order_status")

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def get_ltp(self, security_id: str, exchange_segment: str = "NSE_EQ") -> float:
        """Last traded price for a single instrument."""
        resp = self.dhan.ticker_data({exchange_segment: [int(security_id)]})
        data = self._unwrap(resp, "get_ltp")
        try:
            return float(data["data"][exchange_segment][str(security_id)]["last_price"])
        except (KeyError, TypeError):
            # response shape can vary slightly between SDK versions
            seg = data.get(exchange_segment) or data.get("data", {}).get(exchange_segment, {})
            return float(seg[str(security_id)]["last_price"])

    def get_quote(self, security_id: str, exchange_segment: str = "NSE_EQ") -> dict:
        """Full quote (depth, OHLC, volume) for a single instrument."""
        resp = self.dhan.quote_data({exchange_segment: [int(security_id)]})
        return self._unwrap(resp, "get_quote")

    def get_historical_daily(
        self,
        security_id: str,
        from_date: str,
        to_date: str,
        exchange_segment: str = "NSE_EQ",
        instrument_type: str = "EQUITY",
    ) -> pd.DataFrame:
        """Daily OHLCV candles as a DataFrame. Dates: 'YYYY-MM-DD'."""
        resp = self.dhan.historical_daily_data(
            security_id=str(security_id),
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
        )
        return self._candles_to_df(self._unwrap(resp, "historical_daily"))

    def get_intraday(
        self,
        security_id: str,
        from_date: str,
        to_date: str,
        interval: int = 5,
        exchange_segment: str = "NSE_EQ",
        instrument_type: str = "EQUITY",
    ) -> pd.DataFrame:
        """Intraday candles (interval: 1/5/15/25/60 minutes)."""
        resp = self.dhan.intraday_minute_data(
            security_id=str(security_id),
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
        )
        return self._candles_to_df(self._unwrap(resp, "intraday"))

    @staticmethod
    def _candles_to_df(data: dict) -> pd.DataFrame:
        """Convert Dhan's columnar candle payload into a tidy DataFrame."""
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "timestamp": data.get("timestamp", []),
                "open": data.get("open", []),
                "high": data.get("high", []),
                "low": data.get("low", []),
                "close": data.get("close", []),
                "volume": data.get("volume", []),
            }
        )
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(
                "Asia/Kolkata"
            )
            df = df.set_index("timestamp")
        return df



# ====================================================================== #
# Compatibility layer for dashboard.py and other pages
# ====================================================================== #

# dashboard.py imports the wrapper under this name
DhanHQAPI = DhanAPI

# Popular NSE stocks used across the dashboard pages
STOCK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "ITC", "WIPRO", "AXISBANK", "MARUTI",
    "LT", "HINDUNILVR", "BHARTIARTL", "KOTAKBANK", "BAJFINANCE",
    "ASIANPAINT", "TITAN", "SUNPHARMA", "TATAMOTORS", "TATASTEEL",
]

# Symbol -> Dhan security ID (NSE equity). These are stable, well-known IDs;
# for any symbol not listed here, use DhanAPI.get_security_id(symbol),
# which resolves from Dhan's official instrument master and is authoritative.
STOCK_SECURITY_IDS = {
    "RELIANCE": "2885",
    "TCS": "11536",
    "HDFCBANK": "1333",
    "INFY": "1594",
    "ICICIBANK": "4963",
    "SBIN": "3045",
    "ITC": "1660",
    "WIPRO": "3787",
    "AXISBANK": "5900",
    "MARUTI": "10999",
    "LT": "11483",
    "HINDUNILVR": "1394",
    "BHARTIARTL": "10604",
    "KOTAKBANK": "1922",
    "BAJFINANCE": "317",
    "ASIANPAINT": "236",
    "TITAN": "3506",
    "SUNPHARMA": "3351",
    "TATAMOTORS": "3456",
    "TATASTEEL": "3499",
}

_client_singleton: "DhanAPI | None" = None


def get_dhan_client() -> "DhanAPI | None":
    """
    Return a connected DhanAPI client, or None if no credentials are found.

    Looks for credentials in this order:
      1. config.toml in the working directory (DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN)
      2. Environment variables DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN
      3. Streamlit secrets (DHAN_* or DHANHQ_* key names)
    """
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton

    client_id, token = None, None

    # 1. config.toml
    try:
        import tomllib
        with open("config.toml", "rb") as f:
            cfg = tomllib.load(f)
        client_id = cfg.get("DHAN_CLIENT_ID") or cfg.get("DHANHQ_CLIENT_ID")
        token = cfg.get("DHAN_ACCESS_TOKEN") or cfg.get("DHANHQ_ACCESS_TOKEN")
    except (FileNotFoundError, ImportError):
        pass

    # 2. environment variables
    client_id = client_id or os.getenv("DHAN_CLIENT_ID") or os.getenv("DHANHQ_CLIENT_ID")
    token = token or os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHANHQ_ACCESS_TOKEN")

    # 3. streamlit secrets (only if running inside streamlit)
    if not (client_id and token):
        try:
            import streamlit as st
            client_id = client_id or st.secrets.get("DHAN_CLIENT_ID") or st.secrets.get("DHANHQ_CLIENT_ID")
            token = token or st.secrets.get("DHAN_ACCESS_TOKEN") or st.secrets.get("DHANHQ_ACCESS_TOKEN")
        except Exception:
            pass

    if not (client_id and token):
        logger.warning("get_dhan_client: no credentials found")
        return None

    try:
        _client_singleton = DhanAPI(str(client_id), str(token))
        return _client_singleton
    except Exception as e:
        logger.error("get_dhan_client failed: %s", e)
        return None


if __name__ == "__main__":
    # Quick smoke test (read-only)
    api = DhanAPI()
    print("Funds:", api.get_funds())
    sid = api.get_security_id("RELIANCE")
    print("RELIANCE security id:", sid)
    print(api.get_historical_daily(sid, "2026-06-01", "2026-07-03").tail())
