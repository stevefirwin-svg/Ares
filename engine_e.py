"""
engine_e.py — Ares Engine E: RS Rotation
==========================================
Edge: Capital rotates into sectors with improving relative strength vs SPY.
      Stocks leading their sector while the sector leads SPY are the
      beneficiaries of institutional rotation flows.

Two-layer RS gate:
  Layer 1: Stock RS vs its sector ETF (stock outperforming sector)
  Layer 2: Sector ETF RS vs SPY (sector outperforming the market)
  Both must be positive for Engine E to classify.

Classification gate (ALL must pass):
  1. Symbol not owned by another engine (ownership check FIRST)
  2. Macro: RISK_ON=1.0, NEUTRAL=0.75, RISK_OFF=0.0, CRISIS=0.0
     (RS rotation needs a constructive tape — it's a relative game, not absolute)
  3. e_rs_20d >= 0.03 (stock 20d RS vs sector >= +3%)
  4. e_rs_60d >= 0.05 (stock 60d RS vs sector >= +5% — sustained, not one-day pop)
  5. e_sector_rs_spy >= 0.02 (sector 20d RS vs SPY >= +2%)
  6. e_rs_acceleration >= 0.0 (RS improving recently, not just historically strong)
  7. Engine score >= 0.60

Signals (all prefixed e_):
  e_rs_20d: (stock_return_20d - sector_return_20d)
    Convention: positive = stock outperforming sector over 20 days.
    Range: [-inf, +inf] | Unit: return decimal | Gate: >= 0.03

  e_rs_60d: (stock_return_60d - sector_return_60d)
    Convention: positive = sustained outperformance over 60 days.
    Range: [-inf, +inf] | Unit: return decimal | Gate: >= 0.05

  e_sector_rs_spy: (sector_return_20d - spy_return_20d)
    Convention: positive = sector beating the market.
    Range: [-inf, +inf] | Unit: return decimal | Gate: >= 0.02

  e_rs_acceleration: e_rs_20d - e_rs_60d/3
    Convention: positive = recent RS faster than historical RS trend.
    Range: [-inf, +inf] | Unit: return decimal | Gate: >= 0.0

Stop: 3.0× ATR. RS rotation trades have wider stops — you're riding an institutional
flow that can be volatile in the short term.

Hold: 12–35 days. IC horizon = 20 days (longer than other engines).

IC horizon vs hold duration (RF-11 — accepted design):
  IC horizon (20d) is shorter than the maximum hold (35d). This is intentional:
  - Academic basis: Moskowitz & Grinblatt (1999) use 1-month RS horizon
  - The horizon measures predictive signal power over 20 forward days
  - Trades may be held beyond 20d based on exit conditions, not IC
  - IC anchored at entry+20 days / entry_price (RF-1 convention)
  - Incoherence only arises if IC is measured at exit — it is not (RF-1 fixed)
  - Accepted: IC@20d measures early-phase rotation capture; hold extension is
    exit-logic-driven, not IC-driven. Not a contradiction.

Sector mapping: Uses sector_map.py to assign each symbol to its sector ETF.

Run:
    python engine_e.py --scan
    python engine_e.py --scan --dry
    python engine_e.py --status
"""

import os
import json
import logging
import argparse
import math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv(override=True)

from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    ENGINE_STATUS, ENGINE_REGIME_GATE,
    INITIAL_ENGINE_WEIGHTS, TOTAL_EQUITY,
    KELLY_BOOTSTRAP_FRACTION, KELLY_SHADOW_MULTIPLIER,
    KELLY_ENGINE_CLIPS, ENGINE_STOP_PARAMS,
    CLASSIFICATION_THRESHOLDS, BARS_LOOKBACK,
    SHADOW_MIN_ENTRIES_FOR_LIVE, MAX_POSITIONS_PER_ENGINE,
)
from ledger import Ledger
from margin_guard import check_margin_safety

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EngineE] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/engine_e_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.engine_e")

ENGINE_ID     = "E"
SHADOW_FILE   = "shadow_classifications.json"
MACRO_FILE    = "macro_context.json"
UNIVERSE_FILE = "universe.json"

SECTOR_ETFS = [
    "XLK", "XLF", "XLV", "XLE", "XLY",
    "XLI", "XLB", "XLC", "XLU", "XLRE", "XLP",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


from bars_fetch import fetch_bars as _bars_fetch_shared

def fetch_bars_batch(symbols: List[str], lookback: int = 90) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance.

    Thin wrapper around the shared implementation in bars_fetch.py
    (consolidated 2026-06-28 — see that module's docstring for why).
    """
    if not symbols:
        return {}
    return _bars_fetch_shared(symbols, lookback=lookback, min_bars=65)


def get_account() -> dict:
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = req.get(f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_positions() -> List[dict]:
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = req.get(f"{ALPACA_BASE_URL}/v2/positions", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def submit_order(symbol, qty, client_order_id, limit_price, dry_run=False):
    if dry_run:
        logger.info("[DRY] Would buy %d %s @ %.2f", qty, symbol, limit_price)
        return {"id": "dry-run"}
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }
    body = {
        "symbol":          symbol,
        "qty":             str(qty),
        "side":            "buy",
        "type":            "market",
        "time_in_force":   "day",
        "client_order_id": client_order_id,
    }
    try:
        resp = req.post(f"{ALPACA_BASE_URL}/v2/orders", headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Order failed for %s: %s", symbol, e)
        return None


# ── Sector assignment ─────────────────────────────────────────────────────────

def get_sector_map() -> Dict[str, str]:
    """
    Load sector assignments from sector_map.py if available.
    Falls back to a hardcoded sample map for bootstrap.
    Engine E will classify fewer stocks if sector_map is thin —
    that's correct behavior.
    """
    try:
        import sector_map
        if hasattr(sector_map, "SYMBOL_SECTOR_MAP"):
            return sector_map.SYMBOL_SECTOR_MAP
    except ImportError:
        pass

    # Bootstrap map — top names per sector
    # Expand as universe grows. This is NOT the full map.
    return {
        "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK",
        "META": "XLK", "GOOGL": "XLK", "AVGO": "XLK", "QCOM": "XLK",
        "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
        "UNH": "XLV", "JNJ": "XLV", "PFE": "XLV", "ABBV": "XLV",
        "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE",
        "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY",
        "CAT": "XLI", "DE": "XLI", "RTX": "XLI", "UPS": "XLI",
        "LIN": "XLB", "APD": "XLB", "ECL": "XLB", "NEM": "XLB",
        "NFLX": "XLC", "DIS": "XLC", "CMCSA": "XLC", "T": "XLC",
        "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "AEP": "XLU",
        "PLD": "XLRE", "AMT": "XLRE", "EQIX": "XLRE", "SPG": "XLRE",
        "PG": "XLP", "KO": "XLP", "PEP": "XLP", "WMT": "XLP",
    }


# ── Signals ───────────────────────────────────────────────────────────────────

def _return_n(df: pd.DataFrame, n: int) -> float:
    """N-day price return. Returns 0.0 if insufficient data."""
    if len(df) < n + 1:
        return 0.0
    price_now  = float(df["close"].iloc[-1])
    price_then = float(df["close"].iloc[-n-1])
    if price_then <= 0:
        return 0.0
    return (price_now / price_then) - 1.0


def e_rs_20d(stock_df: pd.DataFrame, sector_df: pd.DataFrame) -> float:
    """
    20-day relative strength: stock return minus sector ETF return.

    Convention: positive = stock outperforming its sector.
    Range: [-inf, +inf] | Unit: return decimal
    Gate: >= 0.03

    Research: Levy (1967), Jegadeesh-Titman (1993) — relative momentum.
    Using sector-relative rather than market-relative removes beta exposure.
    """
    return round(_return_n(stock_df, 20) - _return_n(sector_df, 20), 6)


def e_rs_60d(stock_df: pd.DataFrame, sector_df: pd.DataFrame) -> float:
    """
    60-day relative strength: sustained outperformance window.

    Convention: positive = sustained sector outperformance.
    Range: [-inf, +inf] | Unit: return decimal
    Gate: >= 0.05

    Longer window filters out noise pops. 60d is ~3 months = one full quarter.
    """
    return round(_return_n(stock_df, 60) - _return_n(sector_df, 60), 6)


def e_sector_rs_spy(sector_df: pd.DataFrame, spy_df: pd.DataFrame) -> float:
    """
    Sector 20-day RS vs SPY.

    Convention: positive = sector outperforming market = rotation into this sector.
    Range: [-inf, +inf] | Unit: return decimal
    Gate: >= 0.02

    Research: Faber (2007) — sector rotation as a systematic edge.
    Capital flows at sector level before stock level — this is the leading signal.
    """
    return round(_return_n(sector_df, 20) - _return_n(spy_df, 20), 6)


def e_rs_acceleration(rs_20: float, rs_60: float) -> float:
    """
    RS acceleration: recent RS vs historical RS trend.
    = e_rs_20d - e_rs_60d / 3

    The /3 converts 60d RS to a 20d-equivalent rate for comparison.
    Positive = RS is IMPROVING, not just historically good.

    Convention: positive = relative strength accelerating.
    Range: [-inf, +inf] | Unit: return decimal
    Gate: >= 0.0

    Research: Grinblatt & Han (2005) — momentum acceleration outperforms level.
    """
    return round(rs_20 - rs_60 / 3.0, 6)


def e_rs_new_high(stock_df: pd.DataFrame, sector_df: pd.DataFrame,
                   lookback: int = 60) -> bool:
    """
    Is the 20d RS ratio at a new N-day high?
    True = stock is at its strongest relative performance in `lookback` bars.

    Not a gate signal — logged as context for IC analysis.
    """
    if len(stock_df) < lookback + 21 or len(sector_df) < lookback + 21:
        return False
    # Compute RS ratio over rolling window
    stock_close  = stock_df["close"].tail(lookback + 21)
    sector_close = sector_df["close"].tail(lookback + 21)
    # Align by position (both are sorted ascending)
    min_len = min(len(stock_close), len(sector_close))
    rs_ratio = stock_close.values[-min_len:] / sector_close.values[-min_len:]
    return float(rs_ratio[-1]) >= float(rs_ratio[:-1].max())


def compute_engine_score(rs20: float, rs60: float, sec_rs: float,
                          rs_accel: float, rs_new_high: bool = False) -> float:
    """
    Engine E score ∈ [0, 1].

    Components:
      1. e_rs_20d:          clip(rs20 - 0.03, 0, 0.15) / 0.15
      2. e_rs_60d:          clip(rs60 - 0.05, 0, 0.20) / 0.20
      3. e_sector_rs_spy:   clip(sec_rs - 0.02, 0, 0.10) / 0.10
      4. e_rs_acceleration: clip(rs_accel, 0, 0.05) / 0.05
      5. e_new_rs_high:     binary bonus 0.10 — RS ratio at new N-day high

    RF-14: loads IC-calibrated weights for the four continuous signals.
    RF-18: e_new_rs_high is now correctly named (was e_rs_new_high) and wired into score.
    """
    from ares_weights import load_signal_weights

    s1 = min(1.0, max(0.0, (rs20   - 0.03) / 0.15))
    s2 = min(1.0, max(0.0, (rs60   - 0.05) / 0.20))
    s3 = min(1.0, max(0.0, (sec_rs - 0.02) / 0.10))
    s4 = min(1.0, max(0.0,  rs_accel       / 0.05))
    rs_hi_bonus = 0.10 if rs_new_high else 0.0  # RF-18: small fixed bonus

    weights = load_signal_weights("E")
    w1 = weights.get("e_rs_20d",          0.25)
    w2 = weights.get("e_rs_60d",          0.25)
    w3 = weights.get("e_sector_rs_spy",   0.25)
    w4 = weights.get("e_rs_acceleration", 0.25)
    # e_new_rs_high is binary — handled as fixed bonus, not IC-weighted continuous signal

    signal_score = w1 * s1 + w2 * s2 + w3 * s3 + w4 * s4
    score = min(1.0, signal_score + rs_hi_bonus)
    return round(float(score), 4)


# ── Macro gate ────────────────────────────────────────────────────────────────

def get_macro_multiplier() -> Tuple[float, str]:
    """Engine E: RISK_ON=1.0, NEUTRAL=0.75, RISK_OFF=0.0, CRISIS=0.0"""
    macro  = _load_json(MACRO_FILE, {})
    regime = macro.get("macro_regime", "NEUTRAL")
    probs  = macro.get("regime_probs")
    gates  = ENGINE_REGIME_GATE[ENGINE_ID]

    if probs:
        multiplier = (
            probs.get("RISK_ON",  0) * gates.get("RISK_ON",  1.0) +
            probs.get("NEUTRAL",  0) * gates.get("NEUTRAL",  0.75) +
            probs.get("RISK_OFF", 0) * gates.get("RISK_OFF", 0.0) +
            probs.get("CRISIS",   0) * gates.get("CRISIS",   0.0)
        )
    else:
        multiplier = gates.get(regime, 0.0)

    return round(multiplier, 4), regime


# ── Shadow logging ────────────────────────────────────────────────────────────

def log_shadow(symbol, entry_price, engine_score, signals, macro_regime,
               would_trade, skip_reason=None):
    """Append one shadow classification record to shadow_classifications.json.

    Dedup guard: if engine E already logged this symbol today, skip to prevent
    duplicate records when Task Scheduler fires the engine multiple times in one day.
    """
    shadow = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})
    today = datetime.now().strftime("%Y-%m-%d")

    # Dedup: skip if already logged same engine + symbol + date (non-UNC records only)
    already = any(
        c.get("engine_id") == ENGINE_ID
        and c.get("symbol") == symbol
        and c.get("date") == today
        and "UNC" not in c.get("shadow_id", "")
        for c in shadow.get("classifications", [])
    )
    if already:
        logger.debug("DEDUP SKIP E | %s already logged today — not writing duplicate", symbol)
        return

    record = {
        "shadow_id":         f"E-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":         ENGINE_ID,
        "symbol":            symbol,
        "date":              today,
        "scan_time":         datetime.now().strftime("%H:%M"),
        "entry_price":       round(entry_price, 4),
        "macro_regime":      macro_regime,
        "engine_score":      engine_score,
        "signals":           signals,
        "would_have_traded": would_trade,
        "skip_reason":       skip_reason,
        "outcome_tagged":    False,
        "forward_return_Nd": None,
    }
    shadow["classifications"].append(record)
    _save_json_atomic(SHADOW_FILE, shadow)


def log_unconditioned_shadow(symbol: str, entry_price: float, engine_score: float,
                             signals: dict, macro_regime: str, owned_by: Optional[str]):
    """RF-10 mitigation: log symbols that pass signal gates regardless of ownership."""
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})
    record = {
        "shadow_id":              f"E-UNC-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":              ENGINE_ID,
        "symbol":                 symbol,
        "date":                   datetime.now().strftime("%Y-%m-%d"),
        "scan_time":              datetime.now().strftime("%H:%M"),
        "entry_price":            round(entry_price, 4),
        "macro_regime":           macro_regime,
        "engine_score":           engine_score,
        "signals":                signals,
        "conditioned_by_ownership": False,
        "owned_by":               owned_by,
        "outcome_tagged":         False,
        "forward_return_Nd":      None,
        "note":                   "RF-10 unconditioned log — signal passed without ownership gate",
    }
    shadow_data["classifications"].append(record)
    _save_json_atomic(SHADOW_FILE, shadow_data)


def count_shadow_entries() -> int:
    shadow = _load_json(SHADOW_FILE, {"classifications": []})
    return sum(1 for c in shadow["classifications"] if c.get("engine_id") == ENGINE_ID)


# ── Core scan ─────────────────────────────────────────────────────────────────

def scan(dry_run: bool = False) -> List[dict]:
    status = ENGINE_STATUS.get(ENGINE_ID, "off")
    if status == "off":
        logger.info("Engine E is OFF — skipping scan.")
        return []

    macro_mult, macro_regime = get_macro_multiplier()
    if macro_mult < 0.1:
        logger.info("Engine E gated out: macro=%.3f (%s)", macro_mult, macro_regime)
        return []

    logger.info("Engine E scan | regime=%s | mult=%.3f | mode=%s",
                macro_regime, macro_mult, status)

    universe_data = _load_json(UNIVERSE_FILE, {})
    symbols       = list(universe_data.get("symbols", []))
    if not symbols:
        logger.warning("Universe empty — aborting.")
        return []

    sector_map_data = get_sector_map()
    # Only scan symbols with a known sector assignment
    symbols_with_sector = [s for s in symbols if s in sector_map_data]
    logger.info("Universe: %d symbols, %d with sector assignment",
                len(symbols), len(symbols_with_sector))

    if not symbols_with_sector:
        logger.warning("No symbols with sector assignments — check sector_map.py")
        return []

    ledger    = Ledger()
    ownership = ledger._load_ownership()

    margin_ok, margin_max_new, margin_reason = True, 10_000, "shadow mode — margin guard not enforced"
    try:
        acct         = get_account()
        equity       = float(acct.get("equity", TOTAL_EQUITY))
        buying_power = float(acct.get("buying_power", 0))
        if status == "live":
            try:
                positions = get_positions()
                margin_ok, margin_max_new, margin_reason = check_margin_safety(acct, positions)
                logger.info("Margin guard: %s", margin_reason)
            except Exception as mg_e:
                margin_ok, margin_max_new = False, 0
                margin_reason = f"margin guard check failed (fail closed): {mg_e}"
                logger.error(margin_reason)
    except Exception as e:
        if status == "live":
            logger.error("Account fetch failed in LIVE mode: %s — aborting scan (no fabricated $)", e)
            return []
        logger.warning("Account fetch failed (shadow mode): %s", e)
        equity       = TOTAL_EQUITY
        buying_power = equity * 0.5

    engine_weight    = INITIAL_ENGINE_WEIGHTS[ENGINE_ID]
    engine_ceiling   = equity * engine_weight
    engine_deployed  = ledger.get_engine_capital_deployed(ENGINE_ID)
    engine_remaining = engine_ceiling - engine_deployed
    open_positions   = len(ledger.get_positions(ENGINE_ID))
    max_positions    = MAX_POSITIONS_PER_ENGINE[ENGINE_ID]

    logger.info("Engine E: ceiling=$%.0f deployed=$%.0f remaining=$%.0f positions=%d/%d",
                engine_ceiling, engine_deployed, engine_remaining, open_positions, max_positions)

    # Fetch all bars in one batch: symbols + sector ETFs + SPY
    all_needed = list(set(symbols_with_sector + SECTOR_ETFS + ["SPY"]))
    logger.info("Fetching bars for %d symbols (stocks + sectors + SPY)...", len(all_needed))
    bars_map = fetch_bars_batch(all_needed, lookback=90)
    logger.info("Bars returned: %d/%d", len(bars_map), len(all_needed))

    spy_df = bars_map.get("SPY")
    if spy_df is None:
        logger.error("SPY bars missing — cannot compute sector RS. Aborting.")
        return []

    thresholds  = CLASSIFICATION_THRESHOLDS.get(ENGINE_ID, {})
    score_min   = thresholds.get("engine_score_min", 0.60)
    stop_params = ENGINE_STOP_PARAMS.get(ENGINE_ID, {})
    classified  = []
    entries_this_scan = 0

    for symbol in symbols_with_sector:
        # ── GATE 1: Ownership ─────────────────────────────────────────────────
        if symbol in ownership:
            continue

        stock_df   = bars_map.get(symbol)
        sector_etf = sector_map_data.get(symbol)
        sector_df  = bars_map.get(sector_etf) if sector_etf else None

        if stock_df is None or sector_df is None:
            continue
        if len(stock_df) < 65 or len(sector_df) < 65:
            continue

        price = float(stock_df["close"].iloc[-1])
        atr   = float(stock_df["high"].sub(stock_df["low"]).rolling(14).mean().iloc[-1])
        if atr <= 0 or price <= 0:
            continue

        # ── Signals ───────────────────────────────────────────────────────────
        rs20    = e_rs_20d(stock_df, sector_df)
        rs60    = e_rs_60d(stock_df, sector_df)
        sec_rs  = e_sector_rs_spy(sector_df, spy_df)
        rs_accel = e_rs_acceleration(rs20, rs60)
        rs_hi   = e_rs_new_high(stock_df, sector_df)

        signals = {
            "e_rs_20d":          rs20,
            "e_rs_60d":          rs60,
            "e_sector_rs_spy":   sec_rs,
            "e_rs_acceleration": rs_accel,
            "e_new_rs_high":     rs_hi,    # RF-18: canonical name — matches ENGINE_SIGNALS["E"]
            "e_sector_etf":      sector_etf,
            "atr":               round(atr, 4),
            "price":             round(price, 4),
        }

        # ── GATE 2: Signal thresholds ─────────────────────────────────────────
        if rs20   < 0.03:
            continue
        if rs60   < 0.05:
            continue
        if sec_rs < 0.02:
            continue
        if rs_accel < 0.0:
            continue

        engine_score = compute_engine_score(rs20, rs60, sec_rs, rs_accel, rs_new_high=rs_hi)
        if engine_score < score_min:
            continue

        stop_mult  = stop_params.get("stop_mult_low_vol", 3.0)
        stop_price = round(price - stop_mult * atr, 4)
        hold_target = 20  # IC horizon for Engine E

        # ── GATE 3: Capacity ──────────────────────────────────────────────────
        would_trade = True
        skip_reason = None
        if open_positions >= max_positions:
            would_trade = False
            skip_reason = f"at_position_limit ({open_positions}/{max_positions})"
        if engine_remaining < equity * 0.02:
            would_trade = False
            skip_reason = "engine_capital_exhausted"

        signals["e_stop_price"] = stop_price

        # BUG FIX: previously used `would_trade and (status == "shadow")` which
        # always wrote False once engine is live, silently losing all would_trade=True
        # records and starving IC calibration of valid signal-outcome pairs.
        log_shadow(symbol, price, engine_score, signals, macro_regime,
                   would_trade, skip_reason)

        classified.append({
            "symbol":       symbol,
            "engine_score": engine_score,
            "signals":      signals,
            "would_trade":  would_trade,
            "sector_etf":   sector_etf,
        })

        logger.info(
            "CLASSIFY E | %s [%s] | score=%.3f | rs20=%.3f | rs60=%.3f | "
            "sec_rs=%.3f | accel=%+.3f | %s",
            symbol, sector_etf, engine_score, rs20, rs60, sec_rs, rs_accel,
            "WOULD_TRADE" if would_trade else f"SKIP:{skip_reason}"
        )

        # ── Live entry ────────────────────────────────────────────────────────
        if status == "live" and would_trade and not dry_run:
            if entries_this_scan >= max_positions:
                break

            if not margin_ok:
                logger.info("SKIP %s — margin guard active: %s", symbol, margin_reason)
                continue
            if entries_this_scan >= margin_max_new:
                logger.info("Margin guard cap reached (%d new entries) — stopping entries this scan", margin_max_new)
                break

            min_pct, max_pct = KELLY_ENGINE_CLIPS[ENGINE_ID]
            kelly_scale      = 0.5 + max(0.0, (engine_score - 0.60) / 0.40) * 0.5
            kelly_scale      = round(min(1.0, max(0.5, kelly_scale)) * macro_mult, 4)
            kelly_dollar     = equity * KELLY_BOOTSTRAP_FRACTION * KELLY_SHADOW_MULTIPLIER * kelly_scale
            kelly_dollar     = max(equity * min_pct, min(equity * max_pct, kelly_dollar))
            position_dollar  = min(kelly_dollar, engine_remaining, buying_power * 0.95)

            qty             = max(1, int(position_dollar / price))
            limit_price     = round(price * 1.001, 2)
            client_order_id = f"E_entry_{symbol}_{datetime.now():%Y%m%d%H%M%S}"

            if ledger.is_symbol_owned(symbol):
                continue

            order = submit_order(symbol, qty, client_order_id, limit_price, dry_run=dry_run)
            if order and order.get("id"):
                ledger.record_entry(
                    engine_id   = ENGINE_ID,
                    symbol      = symbol,
                    shares      = qty,
                    entry_price = price,
                    date        = datetime.now().strftime("%Y-%m-%d"),
                    metadata    = {
                        "engine_score":     engine_score,
                        "kelly_scale":      kelly_scale,
                        "macro_mult":       macro_mult,
                        "stop_price":       stop_price,
                        "hold_target_days": hold_target,
                        "sector_etf":       sector_etf,
                        "macro_regime":     macro_regime,
                        "signals_at_entry": signals,
                    }
                )
                logger.info("ENTRY E | %s [%s] | qty=%d | price=%.2f | stop=%.2f",
                            symbol, sector_etf, qty, price, stop_price)
                entries_this_scan += 1
                engine_remaining  -= qty * price
                open_positions    += 1

    shadow_count = count_shadow_entries()
    logger.info("Engine E scan complete | classified=%d | entries=%d | shadow=%d/%d",
                len(classified), entries_this_scan, shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE)

    # ── RF-10: Unconditioned pass — score owned symbols for IC bias correction ─
    unc_logged = 0
    for symbol in list(ownership.keys()):
        if symbol not in sector_map_data:
            continue
        sector_etf = sector_map_data[symbol]
        stock_df   = bars_map.get(symbol)
        sector_df  = bars_map.get(sector_etf)
        if stock_df is None or sector_df is None:
            continue
        if len(stock_df) < 65 or len(sector_df) < 65:
            continue
        price = float(stock_df["close"].iloc[-1])
        atr   = float(stock_df["high"].sub(stock_df["low"]).rolling(14).mean().iloc[-1])
        if atr <= 0 or price <= 0:
            continue
        rs20    = e_rs_20d(stock_df, sector_df)
        rs60    = e_rs_60d(stock_df, sector_df)
        sec_rs  = e_sector_rs_spy(sector_df, spy_df)
        rs_accel = e_rs_acceleration(rs20, rs60)
        if rs20 < 0.03 or rs60 < 0.05 or sec_rs < 0.02 or rs_accel < 0.0:
            continue
        engine_score_unc = compute_engine_score(rs20, rs60, sec_rs, rs_accel)
        if engine_score_unc < score_min:
            continue
        sigs_unc = {
            "e_rs_20d": rs20, "e_rs_60d": rs60, "e_sector_rs_spy": sec_rs,
            "e_rs_acceleration": rs_accel, "e_sector_etf": sector_etf,
            "atr": round(atr, 4), "price": round(price, 4),
        }
        log_unconditioned_shadow(symbol, price, engine_score_unc, sigs_unc,
                                 macro_regime, owned_by=ownership[symbol])
        unc_logged += 1
    if unc_logged:
        logger.info("RF-10: logged %d unconditioned Engine E candidates (owned by others)", unc_logged)

    if status == "shadow" and shadow_count >= SHADOW_MIN_ENTRIES_FOR_LIVE:
        logger.info("ENGINE E SHADOW GATE MET — update ENGINE_STATUS['E']='live'")

    return classified


def print_status():
    ledger       = Ledger()
    positions    = ledger.get_positions(ENGINE_ID)
    shadow_count = count_shadow_entries()
    print(f"\n{'='*60}")
    print(f"  ENGINE E STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Mode:   {ENGINE_STATUS.get(ENGINE_ID,'off').upper()}")
    print(f"  Shadow: {shadow_count}/{SHADOW_MIN_ENTRIES_FOR_LIVE}")
    print(f"  Open:   {len(positions)}")
    for p in positions:
        meta = p.get("metadata", {})
        print(f"    {p['symbol']:8s} [{meta.get('sector_etf','?')}]  "
              f"{p['shares']} shares @ ${p['entry_price']:.2f}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Engine E — RS Rotation")
    parser.add_argument("--scan",   action="store_true")
    parser.add_argument("--dry",    action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print_status()
    elif args.scan:
        results = scan(dry_run=args.dry)
        print(f"\n[Engine E] Scan complete — {len(results)} classification(s)")
    else:
        parser.print_help()
