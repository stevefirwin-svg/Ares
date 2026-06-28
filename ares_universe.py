"""
ares_universe.py — Ares Universe Builder
=========================================
Builds and caches the daily tradeable symbol list for all Ares engines.
Writes universe.json — consumed by all engine scans.

Shares Raptor's screening logic conceptually but runs independently
against the Ares Alpaca account using ARES_ALPACA keys.

Filters (each justified below):
  1. Price $3–$500         — penny stock floor; no fractional-sizing issues
  2. Avg daily $ volume >= $20M (20d) — liquidity for clean fills
  3. Avg daily share volume >= 500K   — confirms institutional participation
  4. ATR/price >= 0.8% (20d)          — minimum dislocation amplitude for MR setups

NOTE: Thresholds are looser than Raptor's deliberately.
  Raptor optimized for momentum + MR combined; tighter filters cut MR candidates.
  Ares Engine B specifically needs names that move — lower $ volume threshold
  allows more mid-cap names where capitulation events are more pronounced.
  TODO:DERIVE — validate filter thresholds against Engine B shadow classification
  frequency. Target: 2–8% of universe classifies on any given day.

Run:
    python ares_universe.py          # rebuild universe.json
    python ares_universe.py --status # show current universe stats
"""

import json
import logging
import os
import time
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

from bars_fetch import fetch_bars
from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    UNIVERSE_PRICE_MIN, UNIVERSE_PRICE_MAX,
    UNIVERSE_MIN_DOLLAR_VOL, UNIVERSE_MIN_SHARE_VOL, UNIVERSE_MIN_ATR_PCT,
)
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [AresUniverse] %(message)s")
logger = logging.getLogger("ares.universe")

UNIVERSE_FILE  = "universe.json"
CACHE_HOURS    = 23   # Rebuild once per day; skip if file is less than 23h old

# RF-21: all filter constants now imported from ares_config.py — single source of truth
PRICE_MIN      = UNIVERSE_PRICE_MIN
PRICE_MAX      = UNIVERSE_PRICE_MAX
MIN_DOLLAR_VOL = UNIVERSE_MIN_DOLLAR_VOL
MIN_SHARE_VOL  = UNIVERSE_MIN_SHARE_VOL
MIN_ATR_PCT    = UNIVERSE_MIN_ATR_PCT

# RF-20: Leveraged, inverse, and crypto-proxy ETFs excluded from all engines.
# Leveraged/inverse products: daily-rebalanced vol decay violates OU mean-reversion
# (Engine B) and their "momentum" is leverage-artifact, not institutional flow (A/E/F).
# Crypto-proxy ETFs: binary event risk and correlation structure don't match equity thesis.
HARD_EXCLUSIONS = {
    "BRK.A", "BRK.B",  # structural (non-standard share classes)
    # Leveraged long ETFs (2x/3x — path-dependent, vol decay)
    "TQQQ", "SOXL", "TECL", "UPRO", "SPXL", "UDOW", "LABU", "CURE",
    "NAIL", "DFEN", "WANT", "HIBL", "MIDU", "URTY", "YINN", "INDL",
    "FNGU", "TSLL", "NVDL", "AMZU", "MSFU", "NVD", "PLTD", "FNGD",
    # Leveraged inverse ETFs (2x/3x short — mean-reversion thesis structurally wrong)
    "SQQQ", "SOXS", "TECS", "SPXS", "SDOW", "LABD", "TZA", "FAZ",
    "SPDN", "SRTY", "YANG", "HIBS", "MIDZ", "BERZ", "SARK",
    # Single-stock leveraged ETFs (ETP wrapper, same issue)
    "TSLL", "TSLS", "AAPD", "AAPU", "AMZU", "AMZD", "MSFU", "MSFD",
    "GOGU", "GOGD", "NVDU", "NVDD",
    # Crypto-proxy ETFs (binary/vol regime mismatch)
    "BITO", "IBIT", "FBTC", "GBTC", "ETHA", "ARKB", "BITB",
}

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_TRADE_URL = ALPACA_BASE_URL


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


# ── Asset list ────────────────────────────────────────────────────────────────

def get_tradeable_assets() -> List[str]:
    """Pull all active, tradeable US equity assets from Alpaca."""
    url    = f"{ALPACA_TRADE_URL}/v2/assets"
    params = {"status": "active", "asset_class": "us_equity", "tradable": True}
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        assets = resp.json()
        symbols = [
            a["symbol"] for a in assets
            if a.get("tradable") and a.get("status") == "active"
            and "/" not in a["symbol"]   # exclude crypto pairs
            and len(a["symbol"]) <= 5    # exclude most warrants/rights
        ]
        logger.info("Alpaca tradeable assets: %d raw symbols", len(symbols))
        return symbols
    except Exception as e:
        logger.error("Asset list fetch failed: %s", e)
        return []


# ── Bar-based screening ───────────────────────────────────────────────────────

def screen_symbols(symbols: List[str]) -> List[str]:
    """
    Screen symbols using 20-day OHLCV bars. Returns passing symbols.

    Uses the shared yfinance bar fetcher (bars_fetch.py) rather than
    Alpaca's /v2/stocks/bars IEX feed — IEX covers only ~2-3% of
    consolidated tape on paper accounts, which was silently dropping
    60-80%+ of candidate symbols before they ever reached the filters
    below (same failure mode as RF-37, fixed elsewhere on 2026-06-19
    but never migrated in this file; confirmed in practice by universe.json
    caching only 144 symbols despite filter thresholds that should pass
    thousands of US equities).
    """
    passing = []
    total   = len(symbols)
    batch_size = 150

    for i in range(0, total, batch_size):
        batch = symbols[i:i+batch_size]
        try:
            bars_by_symbol = fetch_bars(batch, lookback=20, min_bars=15, batch_size=batch_size)
        except Exception as e:
            logger.warning("Bar fetch batch %d/%d failed: %s",
                            i//batch_size+1, (total+batch_size-1)//batch_size, e)
            continue

        for sym, df in bars_by_symbol.items():
            if sym in HARD_EXCLUSIONS or df is None or df.empty:
                continue

            if len(df) < 15:
                continue

            df = df.tail(20)
            price    = float(df["close"].iloc[-1])
            avg_vol  = float(df["volume"].mean())
            avg_dv   = float((df["close"] * df["volume"]).mean())

            # ATR/price
            h, l, c = df["high"], df["low"], df["close"]
            tr = pd.concat([
                h - l,
                (h - c.shift(1)).abs(),
                (l - c.shift(1)).abs()
            ], axis=1).max(axis=1)
            atr     = float(tr.mean())
            atr_pct = atr / price if price > 0 else 0

            # Apply filters
            if price < PRICE_MIN or price > PRICE_MAX:
                continue
            if avg_dv < MIN_DOLLAR_VOL:
                continue
            if avg_vol < MIN_SHARE_VOL:
                continue
            if atr_pct < MIN_ATR_PCT:
                continue

            passing.append(sym)

        logger.info("  Screened batch %d/%d → %d passing so far",
                    i//batch_size+1, (total+batch_size-1)//batch_size, len(passing))
        time.sleep(0.3)  # rate limit buffer

    return sorted(passing)


# ── Build and cache ───────────────────────────────────────────────────────────

def _cache_valid() -> bool:
    if not os.path.exists(UNIVERSE_FILE):
        return False
    mtime = os.path.getmtime(UNIVERSE_FILE)
    age_h = (time.time() - mtime) / 3600
    return age_h < CACHE_HOURS


def build_universe(force: bool = False) -> List[str]:
    if not force and _cache_valid():
        with open(UNIVERSE_FILE) as f:
            data = json.load(f)
        symbols = data.get("symbols", [])
        logger.info("Loaded cached universe: %d symbols (built %s)",
                    len(symbols), data.get("built_at", "?"))
        return symbols

    logger.info("Building Ares universe...")
    raw = get_tradeable_assets()
    if not raw:
        logger.error("No assets returned from Alpaca — aborting")
        return []

    passing = screen_symbols(raw)
    logger.info("Universe built: %d symbols pass all filters (from %d raw)", len(passing), len(raw))

    data = {
        "symbols":      passing,
        "count":        len(passing),
        "built_at":     datetime.now().isoformat(),
        "filters": {
            "price_min":       PRICE_MIN,
            "price_max":       PRICE_MAX,
            "min_dollar_vol":  MIN_DOLLAR_VOL,
            "min_share_vol":   MIN_SHARE_VOL,
            "min_atr_pct":     MIN_ATR_PCT,
        },
    }
    tmp = UNIVERSE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, UNIVERSE_FILE)
    logger.info("universe.json written → %d symbols", len(passing))
    return passing


def print_status():
    if not os.path.exists(UNIVERSE_FILE):
        print("universe.json not found. Run without --status to build.")
        return
    with open(UNIVERSE_FILE) as f:
        data = json.load(f)
    symbols = data.get("symbols", [])
    print(f"\n{'='*50}")
    print(f"  ARES UNIVERSE STATUS")
    print(f"{'='*50}")
    print(f"  Symbols:   {len(symbols)}")
    print(f"  Built at:  {data.get('built_at','?')[:19]}")
    print(f"  Filters:   {data.get('filters', {})}")
    print(f"  Sample:    {symbols[:10]}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Universe Builder")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--force",  action="store_true", help="Force rebuild even if cached")
    args = parser.parse_args()
    if args.status:
        print_status()
    else:
        build_universe(force=args.force)
