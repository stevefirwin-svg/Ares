"""
hold_monitor_e.py — Engine E Hold Health Scoring
==================================================
RS rotation 5-layer health. Engine E holds while sector rotation continues.
Each layer tests whether the rotation thesis is intact.

Layers:

  Layer 1: RS vs sector (35%)
    Is stock still outperforming its sector?
    score = clip((rs_10d - (-0.02)) / 0.07, 0, 1)
    rs_10d = -0.02 → score=0, rs_10d = +0.05 → score=1

  Layer 2: Sector RS vs SPY (25%)
    Is the sector still beating the market?
    score = clip((sec_rs_10d + 0.02) / 0.06, 0, 1)

  Layer 3: Price trend (20%)
    Is price above its 20MA? (Short MA for rotation trades)
    score = 1.0 if above, 0.5 if within 1%, 0.0 if below

  Layer 4: RS acceleration (10%)
    Is RS improving (positive acceleration)?
    score = 0.75 if rs_accel > 0 else 0.25

  Layer 5: Time decay (10%)
    20-day hold target with linear decay.
"""

import logging
import math
from datetime import datetime
from typing import Dict, List

import pandas as pd

logger = logging.getLogger("ares.hold_e")
ENGINE_ID = "E"

LAYER_WEIGHTS = {
    "rs_vs_sector":    0.35,
    "sector_rs_spy":   0.25,
    "price_trend":     0.20,
    "rs_acceleration": 0.10,
    "time_decay":      0.10,
}

TIER_STRENGTHENING = 0.20
TIER_STABLE        = 0.0
TIER_EXHAUSTED     = -0.25


def _return_n(df, n):
    if len(df) < n + 1:
        return 0.0
    p_now  = float(df["close"].iloc[-1])
    p_then = float(df["close"].iloc[-n-1])
    return (p_now / p_then) - 1.0 if p_then > 0 else 0.0


def _days_held(entry_date_str):
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


def _fetch_sector_bars(sector_etf: str) -> pd.DataFrame:
    from ares_config import ALPACA_API_KEY, ALPACA_SECRET_KEY, BAR_FEED
    import requests as req
    from datetime import timedelta
    end   = datetime.now()
    start = end - timedelta(days=30)
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    params = {
        "symbols":   f"{sector_etf},SPY",
        "timeframe": "1Day",
        "start":     start.strftime("%Y-%m-%d"),
        "end":       end.strftime("%Y-%m-%d"),
        "limit":     200,
        "feed":      BAR_FEED,
    }
    try:
        resp = req.get("https://data.alpaca.markets/v2/stocks/bars",
                       headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data  = resp.json().get("bars", {})
        result = {}
        for sym, bars in data.items():
            if bars:
                df = pd.DataFrame(bars)
                df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
                result[sym] = df
        return result.get(sector_etf), result.get("SPY")
    except Exception:
        return None, None


def _classify_tier(score):
    if score >= TIER_STRENGTHENING:
        return "STRENGTHENING"
    elif score >= TIER_STABLE:
        return "STABLE"
    elif score >= TIER_EXHAUSTED:
        return "DECAYING"
    else:
        return "EXHAUSTED"


def score_engine_e_positions(positions, bars_map, sector_cache=None):
    health_records = []
    if sector_cache is None:
        sector_cache = {}

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = float(pos.get("entry_price", 0))
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0:
            continue

        stock_df = bars_map.get(symbol)
        if stock_df is None or len(stock_df) < 12:
            health_records.append({
                "symbol": symbol, "engine_id": ENGINE_ID,
                "tier": "INSUFFICIENT_DATA", "score": None, "layers": {},
            })
            continue

        sector_etf = meta.get("sector_etf", "XLK")
        if sector_etf not in sector_cache:
            sec_df, spy_df = _fetch_sector_bars(sector_etf)
            sector_cache[sector_etf] = (sec_df, spy_df)
        else:
            sec_df, spy_df = sector_cache[sector_etf]

        price       = float(stock_df["close"].iloc[-1])
        days        = _days_held(entry_date)
        hold_target = int(meta.get("hold_target_days", 20))
        ma20        = float(stock_df["close"].rolling(20).mean().iloc[-1])
        pnl_pct     = round(((price / entry_price) - 1) * 100, 2)

        # RS signals
        stock_ret10  = _return_n(stock_df, 10)
        sector_ret10 = _return_n(sec_df,   10) if sec_df is not None else 0.0
        spy_ret10    = _return_n(spy_df,    10) if spy_df is not None else 0.0
        rs_10d       = stock_ret10 - sector_ret10
        sec_rs_10d   = sector_ret10 - spy_ret10

        # Entry RS for acceleration comparison
        entry_sigs  = meta.get("signals_at_entry", {})
        entry_rs20  = float(entry_sigs.get("e_rs_20d", 0) or 0)
        entry_rs60  = float(entry_sigs.get("e_rs_60d", 0) or 0)
        rs_accel_now = rs_10d - (entry_rs60 / 6.0 if entry_rs60 else 0.0)

        # Layer scores
        l1 = min(1.0, max(0.0, (rs_10d    + 0.02) / 0.07))
        l2 = min(1.0, max(0.0, (sec_rs_10d + 0.02) / 0.06))
        if price > ma20 * 1.01:
            l3 = 1.0
        elif price > ma20 * 0.99:
            l3 = 0.5
        else:
            l3 = 0.0
        l4 = 0.75 if rs_accel_now > 0 else 0.25
        l5 = max(0.0, 1.0 - days / hold_target) if hold_target > 0 else 0.5

        layers = {
            "rs_vs_sector":    round(l1, 4),
            "sector_rs_spy":   round(l2, 4),
            "price_trend":     round(l3, 4),
            "rs_acceleration": round(l4, 4),
            "time_decay":      round(l5, 4),
        }

        raw          = sum(LAYER_WEIGHTS[k] * v for k, v in layers.items())
        health_score = round((raw - 0.5) * 2.0, 4)
        tier         = _classify_tier(health_score)

        record = {
            "symbol":     symbol,
            "engine_id":  ENGINE_ID,
            "tier":       tier,
            "score":      health_score,
            "pnl_pct":    pnl_pct,
            "days_held":  days,
            "hold_target": hold_target,
            "sector_etf": sector_etf,
            "rs_10d":     round(rs_10d, 4),
            "sec_rs_10d": round(sec_rs_10d, 4),
            "layers":     layers,
            "scored_at":  datetime.now().isoformat(),
        }
        health_records.append(record)

        logger.info("HOLD E | %s [%s] | tier=%s | score=%+.3f | pnl=%.2f%% | days=%d/%d",
                    symbol, sector_etf, tier, health_score, pnl_pct, days, hold_target)

    return health_records
