"""
engine_c.py — Ares Engine C: Squeeze Break
===========================================
Edge: Compression energy release. A TTM Squeeze (Bollinger Bands inside Keltner
      Channels) stores kinetic energy. When it fires, the directional move is rapid.
      Engine C captures the first 3 bars of the fire — then confirms or exits.

Classification gate (ALL must pass):
  1. Symbol not owned by another engine (ownership check FIRST)
  2. Macro not CRISIS (C fires in all other regimes, scaled by multiplier)
     RISK_ON=1.0, NEUTRAL=0.90, RISK_OFF=0.50, CRISIS=0.0
  3. c_squeeze_duration >= 5 bars (compression built up enough to be meaningful)
  4. c_hv_ratio <= 0.80 (HV/IV ratio — volatility compressed, energy stored)
  5. c_momentum_direction == True (TTM momentum histogram turning positive on fire bar)
  6. Engine score >= 0.60

Phase state:
  COMPRESSING  — squeeze active, not yet fired
  FIRING       — squeeze released this bar (entry point)
  CONFIRMING   — 1-2 bars post-fire, checking direction holds
  COMPLETE     — exited (3 bars elapsed or wrong direction)

Signals (all prefixed c_):
  c_squeeze_duration: int
    Number of consecutive bars where BB is inside KC (squeeze active).
    Convention: higher = more energy stored = stronger potential move.
    Range: [0, inf] | Unit: bars | Gate: >= 5

  c_hv_ratio: float
    Historical volatility (20d) / Implied volatility proxy (ATR/price × √252).
    Convention: lower = more compression relative to realized vol.
    Range: [0, inf] | Unit: ratio | Gate: <= 0.80
    < 0.80 = HV suppressed below ATR-implied vol = squeeze is real

  c_momentum_direction: bool
    TTM momentum histogram (close - midpoint of Bollinger/Keltner) > 0 on fire bar.
    Convention: True = momentum turning positive = long bias on squeeze fire.
    Gate: must be True (we only go long; short squeeze not implemented)

Stop model:
  1.5–1.8× ATR (tightest stop of all engines — squeeze fires are decisive or wrong).
  If the move doesn't work in 3 bars, it's not working. Time stop = 3 bars.

Hold:
  3 bars minimum. Extends if confirming (direction holds and momentum builds).
  Maximum: 8 bars. Beyond 8 bars the squeeze edge has expired.

Run:
    python engine_c.py --scan
    python engine_c.py --scan --dry
    python engine_c.py --status
"""

import os
import json
import logging
import argparse
import math
import numpy as np
import pandas as pd
from datetime import datetime
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

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EngineC] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/engine_c_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.engine_c")

ENGINE_ID     = "C"
SHADOW_FILE   = "shadow_classifications.json"
MACRO_FILE    = "macro_context.json"
UNIVERSE_FILE = "universe.json"

# Squeeze parameters
BB_PERIOD     = 20
BB_MULT       = 2.0
KC_PERIOD     = 20
KC_MULT       = 1.5   # Keltner multiplier (ATR-based)
MIN_SQUEEZE_BARS = 5  # minimum compression duration for valid squeeze
MAX_HOLD_BARS    = 8  # maximum hold regardless of direction
HV_LOOKBACK      = 20 # historical vol lookback


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


def fetch_bars(symbols: List[str], lookback: int = BARS_LOOKBACK) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance.

    Replaces Alpaca/IEX bar endpoint which only covers ~2-3% of consolidated
    tape on paper accounts, causing 60-80% of universe symbols to return no data.
    yfinance covers all symbols with 5+ years of history at no cost.
    Order submission remains on Alpaca (unaffected).
    """
    import yfinance as yf
    from datetime import timedelta

    if not symbols:
        return {}
    end   = datetime.now()
    start = end - timedelta(days=int(lookback * 1.6))
    bars_out = {}

    for i in range(0, len(symbols), 100):
        batch = symbols[i:i+100]
        try:
            raw = yf.download(
                tickers=batch,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                continue

            if isinstance(raw.columns, pd.MultiIndex):
                for sym in batch:
                    try:
                        df = raw.xs(sym, axis=1, level=1).copy()
                        df.columns = [c.lower() for c in df.columns]
                        df = df.dropna(subset=["close"])
                        df["timestamp"] = df.index
                        df = df.reset_index(drop=True).tail(lookback)
                        if len(df) >= BB_PERIOD + 5:
                            bars_out[sym] = df
                    except Exception:
                        continue
            else:
                sym = batch[0]
                df = raw.copy()
                df.columns = [c.lower() for c in df.columns]
                df = df.dropna(subset=["close"])
                df["timestamp"] = df.index
                df = df.reset_index(drop=True).tail(lookback)
                if len(df) >= BB_PERIOD + 5:
                    bars_out[sym] = df

        except Exception as e:
            logger.warning("yfinance bar fetch failed (batch %d): %s", i // 100, e)

    return bars_out


def get_account() -> dict:
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = req.get(f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def submit_order(symbol: str, qty: int, client_order_id: str,
                 limit_price: float, dry_run: bool = False) -> Optional[dict]:
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


# ── Indicators ────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


def _bollinger_bands(df: pd.DataFrame, period: int = BB_PERIOD,
                      mult: float = BB_MULT) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, mid, lower) Bollinger Bands."""
    mid   = df["close"].rolling(period).mean()
    std   = df["close"].rolling(period).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


def _keltner_channels(df: pd.DataFrame, period: int = KC_PERIOD,
                       mult: float = KC_MULT) -> Tuple[pd.Series, pd.Series]:
    """Returns (upper, lower) Keltner Channels using ATR."""
    h, l, c = df["high"], df["low"], df["close"]
    tr  = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    mid = c.rolling(period).mean()
    return mid + mult * atr, mid - mult * atr


# ── Signals ───────────────────────────────────────────────────────────────────

def compute_squeeze_signals(df: pd.DataFrame) -> dict:
    """
    Compute all Engine C signals. Returns dict with squeeze state and metrics.

    TTM Squeeze detection:
      squeeze_on[t] = True when BB_upper[t] < KC_upper[t] AND BB_lower[t] > KC_lower[t]
      i.e. Bollinger Bands are entirely inside the Keltner Channels.

    Squeeze fired: squeeze_on was True, now False (or first non-squeeze bar after run).

    c_squeeze_duration:
      Count of consecutive bars where squeeze_on == True ENDING at bar[-2] (yesterday).
      We need the squeeze to have JUST fired on bar[-1] (today).
      Duration = length of the streak that ended.

    c_hv_ratio:
      HV20 / (ATR_14 / price * sqrt(252))
      HV20 = rolling 20-day standard deviation of log returns × sqrt(252) = annualized HV
      ATR/price × sqrt(252) = annualized ATR-implied vol proxy
      Lower ratio = more compression.

    c_momentum_direction:
      TTM momentum = close - midpoint of (highest_high_N + lowest_low_N)/2 + BB_mid)/2
      Positive = bullish momentum, negative = bearish.
      We only enter long when momentum is positive on the fire bar.

    Research: Carter (2006) TTM Squeeze — momentum oscillator built on
    price position relative to the BB/KC midpoints.
    """
    if len(df) < BB_PERIOD + 5:
        return {"valid": False}

    bb_upper, bb_mid, bb_lower = _bollinger_bands(df)
    kc_upper, kc_lower         = _keltner_channels(df)

    # Squeeze on/off series
    squeeze_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)

    # Check: squeeze fired on the LAST bar (squeeze_on[-2] == True, squeeze_on[-1] == False)
    if len(squeeze_on) < 3:
        return {"valid": False}

    was_squeezing = bool(squeeze_on.iloc[-2])
    is_squeezing  = bool(squeeze_on.iloc[-1])
    fired_today   = was_squeezing and not is_squeezing

    # Duration: count consecutive True values ending at iloc[-2]
    sq_arr    = squeeze_on.values
    duration  = 0
    idx       = len(sq_arr) - 2  # start at yesterday
    while idx >= 0 and sq_arr[idx]:
        duration += 1
        idx -= 1

    # c_hv_ratio
    log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()
    hv20    = float(log_ret.tail(HV_LOOKBACK).std() * math.sqrt(252)) if len(log_ret) >= HV_LOOKBACK else None
    atr14   = _atr(df)
    price   = float(df["close"].iloc[-1])
    atr_vol = (atr14 / price * math.sqrt(252)) if price > 0 else None
    hv_ratio = (hv20 / atr_vol) if (hv20 and atr_vol and atr_vol > 0) else None

    # c_momentum_direction (TTM momentum oscillator)
    n       = BB_PERIOD
    highest = df["high"].rolling(n).max()
    lowest  = df["low"].rolling(n).min()
    mid_hl  = (highest + lowest) / 2
    ttm_mom = df["close"] - (mid_hl + bb_mid) / 2
    mom_now  = float(ttm_mom.iloc[-1])
    mom_prev = float(ttm_mom.iloc[-2]) if len(ttm_mom) >= 2 else 0.0
    momentum_positive = mom_now > 0
    momentum_rising   = mom_now > mom_prev

    return {
        "valid":                True,
        "fired_today":          fired_today,
        "was_squeezing":        was_squeezing,
        "is_squeezing":         is_squeezing,
        "c_squeeze_duration":   duration,
        "c_hv_ratio":           round(hv_ratio, 4) if hv_ratio else None,
        "c_momentum_direction": momentum_positive,
        "c_momentum_rising":    momentum_rising,
        "c_mom_value":          round(mom_now, 6),
        "atr":                  round(atr14, 4),
        "price":                round(price, 4),
    }


def compute_engine_score(duration: int, hv_ratio: float,
                          mom_dir: bool, mom_rising: bool) -> float:
    """
    Engine C score ∈ [0, 1].

    Components:
      1. c_squeeze_duration: clip(duration - 5, 0, 15) / 15
      2. c_hv_ratio (compression): clip(1 - hv_ratio, 0, 0.5) / 0.5
      3. c_momentum_direction: binary
      4. mom_rising (unregistered gate): binary — not IC-calibrated, used equally

    RF-14: loads IC-calibrated weights for the three declared signals (c_squeeze_duration,
    c_hv_ratio, c_momentum_direction). mom_rising contributes a fixed 0.15 structural bonus.
    """
    from ares_weights import load_signal_weights

    dur_score  = min(1.0, max(0.0, (duration - 5) / 15.0))
    comp_score = min(1.0, max(0.0, (1.0 - hv_ratio) / 0.5)) if hv_ratio else 0.5
    mom_score  = 1.0 if mom_dir else 0.0
    rise_bonus = 0.15 if mom_rising else 0.0  # fixed structural bonus, not in IC registry

    weights = load_signal_weights("C")
    w_dur  = weights.get("c_squeeze_duration",   1.0 / 3)
    w_comp = weights.get("c_hv_ratio",           1.0 / 3)
    w_mom  = weights.get("c_momentum_direction", 1.0 / 3)

    # Signal score weighted by IC; rise_bonus is a small fixed additive
    signal_score = w_dur * dur_score + w_comp * comp_score + w_mom * mom_score
    score = min(1.0, signal_score + rise_bonus)
    return round(float(score), 4)


# ── Macro gate ────────────────────────────────────────────────────────────────

def get_macro_multiplier() -> Tuple[float, str]:
    """Engine C: RISK_ON=1.0, NEUTRAL=0.90, RISK_OFF=0.50, CRISIS=0.0"""
    macro  = _load_json(MACRO_FILE, {})
    regime = macro.get("macro_regime", "NEUTRAL")
    probs  = macro.get("regime_probs")
    gates  = ENGINE_REGIME_GATE[ENGINE_ID]

    if probs:
        multiplier = (
            probs.get("RISK_ON",  0) * gates.get("RISK_ON",  1.0) +
            probs.get("NEUTRAL",  0) * gates.get("NEUTRAL",  0.90) +
            probs.get("RISK_OFF", 0) * gates.get("RISK_OFF", 0.50) +
            probs.get("CRISIS",   0) * gates.get("CRISIS",   0.0)
        )
    else:
        multiplier = gates.get(regime, 0.0)

    return round(multiplier, 4), regime


# ── Shadow logging ────────────────────────────────────────────────────────────

def log_shadow(symbol: str, entry_price: float, engine_score: float,
               signals: dict, macro_regime: str, would_trade: bool,
               skip_reason: Optional[str] = None):
    shadow = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})
    record = {
        "shadow_id":         f"C-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":         ENGINE_ID,
        "symbol":            symbol,
        "date":              datetime.now().strftime("%Y-%m-%d"),
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
        "shadow_id":              f"C-UNC-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
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
        logger.info("Engine C is OFF — skipping scan.")
        return []

    macro_mult, macro_regime = get_macro_multiplier()
    if macro_mult < 0.05:
        logger.info("Engine C gated out: macro=%.3f (%s)", macro_mult, macro_regime)
        return []

    logger.info("Engine C scan | regime=%s | mult=%.3f | mode=%s",
                macro_regime, macro_mult, status)

    universe_data = _load_json(UNIVERSE_FILE, {})
    symbols       = list(universe_data.get("symbols", []))
    if not symbols:
        logger.warning("Universe empty — aborting.")
        return []

    ledger    = Ledger()
    ownership = ledger._load_ownership()

    try:
        acct         = get_account()
        equity       = float(acct.get("equity", TOTAL_EQUITY))
        buying_power = float(acct.get("buying_power", 0))
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

    logger.info("Engine C: ceiling=$%.0f deployed=$%.0f remaining=$%.0f positions=%d/%d",
                engine_ceiling, engine_deployed, engine_remaining, open_positions, max_positions)

    thresholds  = CLASSIFICATION_THRESHOLDS.get(ENGINE_ID, {})
    score_min   = thresholds.get("engine_score_min", 0.60)
    stop_params = ENGINE_STOP_PARAMS.get(ENGINE_ID, {})
    classified  = []
    entries_this_scan = 0

    logger.info("Fetching bars for %d symbols...", len(symbols))
    bars_map = fetch_bars(symbols, lookback=BARS_LOOKBACK)
    logger.info("Bars returned: %d/%d", len(bars_map), len(symbols))

    for symbol, df in bars_map.items():
        # ── GATE 1: Ownership ─────────────────────────────────────────────────
        if symbol in ownership:
            continue

        sigs = compute_squeeze_signals(df)
        if not sigs.get("valid"):
            continue

        # ── GATE 2: Squeeze must have fired today ─────────────────────────────
        if not sigs["fired_today"]:
            continue

        duration = sigs["c_squeeze_duration"]
        hv_ratio = sigs["c_hv_ratio"]
        mom_dir  = sigs["c_momentum_direction"]
        price    = sigs["price"]
        atr      = sigs["atr"]

        if duration < MIN_SQUEEZE_BARS:
            continue
        if hv_ratio is None or hv_ratio > 0.80:
            continue
        if not mom_dir:
            continue

        engine_score = compute_engine_score(duration, hv_ratio, mom_dir,
                                             sigs["c_momentum_rising"])
        if engine_score < score_min:
            continue

        stop_mult  = stop_params.get("stop_mult_low_vol", 1.5)
        stop_price = round(price - stop_mult * atr, 4)

        signals = {
            "c_squeeze_duration":   duration,
            "c_hv_ratio":           hv_ratio,
            "c_momentum_direction": mom_dir,
            "c_momentum_rising":    sigs["c_momentum_rising"],
            "c_mom_value":          sigs["c_mom_value"],
            "c_stop_price":         stop_price,
            "atr":                  atr,
            "price":                price,
        }

        # ── GATE 3: Capacity ──────────────────────────────────────────────────
        would_trade = True
        skip_reason = None
        if open_positions >= max_positions:
            would_trade = False
            skip_reason = f"at_position_limit ({open_positions}/{max_positions})"
        if engine_remaining < equity * 0.02:
            would_trade = False
            skip_reason = "engine_capital_exhausted"

        log_shadow(symbol, price, engine_score, signals, macro_regime,
                   would_trade and (status == "shadow"), skip_reason)

        classified.append({
            "symbol":      symbol,
            "engine_score": engine_score,
            "signals":     signals,
            "would_trade": would_trade,
        })

        logger.info(
            "CLASSIFY C | %s | score=%.3f | squeeze_dur=%d | hv_ratio=%.2f | "
            "mom=%+.4f | %s",
            symbol, engine_score, duration, hv_ratio,
            sigs["c_mom_value"],
            "WOULD_TRADE" if would_trade else f"SKIP:{skip_reason}"
        )

        # ── Live entry ────────────────────────────────────────────────────────
        if status == "live" and would_trade and not dry_run:
            if entries_this_scan >= max_positions:
                break

            min_pct, max_pct = KELLY_ENGINE_CLIPS[ENGINE_ID]
            kelly_scale      = 0.5 + max(0.0, (engine_score - 0.60) / 0.40) * 0.5
            kelly_scale      = round(min(1.0, max(0.5, kelly_scale)) * macro_mult, 4)
            kelly_dollar     = equity * KELLY_BOOTSTRAP_FRACTION * KELLY_SHADOW_MULTIPLIER * kelly_scale
            kelly_dollar     = max(equity * min_pct, min(equity * max_pct, kelly_dollar))
            position_dollar  = min(kelly_dollar, engine_remaining, buying_power * 0.95)

            qty             = max(1, int(position_dollar / price))
            limit_price     = round(price * 1.001, 2)
            client_order_id = f"C_entry_{symbol}_{datetime.now():%Y%m%d%H%M%S}"

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
                        "stop_price":       stop_price,
                        "hold_target_days": 3,   # initial; extends on confirm
                        "max_hold_days":    MAX_HOLD_BARS,
                        "macro_regime":     macro_regime,
                        "phase":            "FIRING",
                        "signals_at_entry": signals,
                    }
                )
                logger.info("ENTRY C | %s | qty=%d | price=%.2f | stop=%.2f",
                            symbol, qty, price, stop_price)
                entries_this_scan += 1
                engine_remaining  -= qty * price
                open_positions    += 1

    shadow_count = count_shadow_entries()
    logger.info("Engine C scan complete | classified=%d | entries=%d | shadow=%d/%d",
                len(classified), entries_this_scan, shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE)

    # ── RF-10: Unconditioned pass ─────────────────────────────────────────────
    unc_logged = 0
    _score_min = thresholds.get("engine_score_min", 0.60)
    for symbol, df in bars_map.items():
        if symbol not in ownership:
            continue
        sigs = compute_squeeze_signals(df)
        if not sigs.get("valid") or not sigs.get("fired_today"):
            continue
        duration = sigs["c_squeeze_duration"]
        hv_ratio = sigs["c_hv_ratio"]
        mom_dir  = sigs["c_momentum_direction"]
        if duration < MIN_SQUEEZE_BARS or hv_ratio is None or hv_ratio > 0.80 or not mom_dir:
            continue
        engine_score_unc = compute_engine_score(duration, hv_ratio, mom_dir, sigs["c_momentum_rising"])
        if engine_score_unc < _score_min:
            continue
        sigs_unc = {
            "c_squeeze_duration": duration, "c_hv_ratio": hv_ratio,
            "c_momentum_direction": mom_dir, "atr": sigs.get("atr", 0),
            "price": sigs.get("price", 0),
        }
        log_unconditioned_shadow(symbol, sigs["price"], engine_score_unc, sigs_unc,
                                 macro_regime, owned_by=ownership[symbol])
        unc_logged += 1
    if unc_logged:
        logger.info("RF-10: logged %d unconditioned Engine C candidates (owned by others)", unc_logged)

    if status == "shadow" and shadow_count >= SHADOW_MIN_ENTRIES_FOR_LIVE:
        logger.info("ENGINE C SHADOW GATE MET — update ENGINE_STATUS['C']='live'")

    return classified


def print_status():
    ledger       = Ledger()
    positions    = ledger.get_positions(ENGINE_ID)
    shadow_count = count_shadow_entries()
    print(f"\n{'='*60}")
    print(f"  ENGINE C STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Mode:   {ENGINE_STATUS.get(ENGINE_ID,'off').upper()}")
    print(f"  Shadow: {shadow_count}/{SHADOW_MIN_ENTRIES_FOR_LIVE}")
    print(f"  Open:   {len(positions)}")
    for p in positions:
        print(f"    {p['symbol']:8s}  {p['shares']} shares @ ${p['entry_price']:.2f}"
              f"  phase={p.get('metadata',{}).get('phase','?')}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Engine C — Squeeze Break")
    parser.add_argument("--scan",   action="store_true")
    parser.add_argument("--dry",    action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print_status()
    elif args.scan:
        results = scan(dry_run=args.dry)
        print(f"\n[Engine C] Scan complete — {len(results)} classification(s)")
    else:
        parser.print_help()
