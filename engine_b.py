"""
engine_b.py — Ares Engine B: Mean Reversion / Capitulation
===========================================================
Edge: OU (Ornstein-Uhlenbeck) pull to equilibrium after a fear-driven selloff.
      Price has overshot fair value; statistical reversion is the thesis.

Classification gate (ALL must pass):
  1. Symbol not owned by another engine (ownership check FIRST)
  2. Macro regime not CRISIS (B fires in RISK_OFF/NEUTRAL at full scale;
     scaled by ENGINE_REGIME_GATE multiplier — no cliff switch)
  3. b_avwap_distance <= -1.0 ATR  (price meaningfully below AVWAP)
  4. b_stoch_divergence == True     (stochastic oversold + positive divergence)
  5. b_volume_climax >= 1.5         (recent capitulation volume spike)
  6. Engine score >= CLASSIFICATION_THRESHOLDS["B"]["engine_score_min"] (0.60)

Sizing:
  position_$ = min(
      equity × KELLY_BOOTSTRAP_FRACTION × KELLY_SHADOW_MULTIPLIER × kelly_scale,
      engine_capital_remaining,
      buying_power × 0.95
  )
  kelly_scale ∈ [0.5, 1.0] — derived from engine_score percentile vs shadow history

OU theta:
  Estimated via 30-day rolling OLS on log-price.
  hold_target = OU_half_life × 1.5 (where half_life = ln(2) / theta)
  TODO:DERIVE — theta window 30d validated against Granger causality test

Signals (all prefixed b_):
  b_avwap_distance: (price - AVWAP) / ATR
    Convention: negative = below AVWAP. Stronger signal when more negative.
    Range: [-inf, +inf] | Unit: ATR multiples | Positive means: above AVWAP

  b_stoch_divergence: bool
    True when stochastic %K < 20 AND current low > prior swing low (positive divergence)
    Convention: True = oversold with positive price divergence

  b_volume_climax: float
    Current bar volume / 20d average volume
    Convention: higher = stronger capitulation signal
    Range: [0, inf] | Unit: ratio vs avg | >1.5 suggests climax selling

Shadow mode: ENGINE_STATUS["B"] == "shadow" → classify but do not trade.
All classifications written to shadow_classifications.json.
Gate: 30 shadow classifications before live capital.

Run:
    python engine_b.py --scan        # Run full scan
    python engine_b.py --scan --dry  # Dry run (no orders, no shadow write)
    python engine_b.py --status      # Show current B positions
"""

import os
import json
import logging
import argparse
import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from dotenv import load_dotenv
load_dotenv()

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
    format="%(asctime)s [EngineB] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/engine_b_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.engine_b")

ENGINE_ID          = "B"
SHADOW_FILE        = "shadow_classifications.json"
MACRO_FILE         = "macro_context.json"
UNIVERSE_FILE      = "universe.json"

# ── Data helpers ──────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json_atomic(path: str, data):
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
                        if len(df) >= 30:
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
                if len(df) >= 30:
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


def get_buying_power() -> float:
    try:
        acct = get_account()
        return float(acct.get("buying_power", 0))
    except Exception:
        return 0.0


def submit_order(symbol: str, qty: int, client_order_id: str,
                 limit_price: float, dry_run: bool = False) -> Optional[dict]:
    if dry_run:
        logger.info("[DRY] Would buy %d shares of %s @ %.2f", qty, symbol, limit_price)
        return {"id": "dry-run", "symbol": symbol, "qty": qty}

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
        "type":            "limit",
        "time_in_force":   "day",
        "limit_price":     str(round(limit_price, 2)),
        "client_order_id": client_order_id,
    }
    try:
        resp = req.post(f"{ALPACA_BASE_URL}/v2/orders", headers=headers,
                        json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Order submit failed for %s: %s", symbol, e)
        return None


# ── ATR ───────────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


# ── Signal computation ────────────────────────────────────────────────────────

def compute_avwap(df: pd.DataFrame, lookback: int = 20) -> float:
    """
    Anchored VWAP over a rolling lookback window.
    Uses close × volume as the anchor; AVWAP = cumulative(close×vol) / cumulative(vol).
    TODO: Extend to anchor at last swing high/low once swing detection is built.
    """
    window = df.tail(lookback)
    if window.empty or window["volume"].sum() == 0:
        return float(df["close"].iloc[-1])
    # Use vwap if available (Alpaca provides it), else compute from OHLCV
    if "vwap" in window.columns and window["vwap"].notna().all():
        vwap_col = window["vwap"]
    else:
        vwap_col = (window["high"] + window["low"] + window["close"]) / 3.0
    avwap = (vwap_col * window["volume"]).sum() / window["volume"].sum()
    return float(avwap)


def b_avwap_distance(df: pd.DataFrame, atr: float) -> float:
    """
    (price - AVWAP) / ATR

    Convention: negative = below AVWAP (MR buy signal when negative).
    Range: [-inf, +inf] | Unit: ATR multiples
    Positive means: price above AVWAP (no MR edge).
    Gate threshold: <= -1.0 (price at least 1 ATR below AVWAP).
    """
    if atr <= 0:
        return 0.0
    price = float(df["close"].iloc[-1])
    avwap = compute_avwap(df)
    return round((price - avwap) / atr, 4)


def b_stoch_divergence(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> Tuple[bool, float]:
    """
    Stochastic %K (fast) + positive price divergence.

    Returns (divergence_flag, stoch_k_value)
    divergence_flag = True when:
      1. %K < 20 (oversold)
      2. Current bar's low > prior swing low (price making higher low)
         where prior swing low = minimum low over bars [-20:-5]

    Convention: True = oversold + positive divergence = MR entry signal.

    Research basis: Stochastic-price divergence as confirmation of exhaustion
    (Elder, 1993; Murphy, 1999 technical analysis canon).
    """
    if len(df) < k_period + d_period + 5:
        return False, 50.0

    low_k  = df["low"].rolling(k_period).min()
    high_k = df["high"].rolling(k_period).max()
    denom  = high_k - low_k
    denom  = denom.replace(0, np.nan)
    stoch_k = ((df["close"] - low_k) / denom * 100).fillna(50)

    k_now    = float(stoch_k.iloc[-1])
    oversold = k_now < 20.0

    # Positive divergence: current low > prior swing low
    if len(df) >= 20:
        prior_swing_low = float(df["low"].iloc[-20:-5].min())
        current_low     = float(df["low"].iloc[-1])
        positive_div    = current_low > prior_swing_low
    else:
        positive_div = False

    return (oversold and positive_div), round(k_now, 2)


def b_volume_climax(df: pd.DataFrame, lookback: int = 20) -> float:
    """
    Current bar volume / 20-day average volume.

    Convention: higher = stronger capitulation signal.
    Range: [0, inf] | Unit: ratio | > 1.5 suggests climax selling.
    Gate threshold: >= 1.5.

    Rationale: Climax volume marks selling exhaustion; high-volume
    down bars on MR setups historically have better reversion rates
    (Connors & Alvarez, 2009; mean reversion volume literature).
    """
    if len(df) < lookback + 1:
        return 0.0
    avg_vol  = float(df["volume"].iloc[-lookback-1:-1].mean())
    cur_vol  = float(df["volume"].iloc[-1])
    if avg_vol <= 0:
        return 0.0
    return round(cur_vol / avg_vol, 4)


def estimate_ou_theta(df: pd.DataFrame, window: int = 30) -> Tuple[float, float]:
    """
    Estimate OU mean-reversion speed (theta) via OLS on log-price differences.

    Model: d(log P) = theta × (mu - log P) × dt + sigma × dW
    OLS: regress log_price[t] - log_price[t-1] on log_price[t-1]
    Coefficient on log_price[t-1] = -theta × dt

    Returns (theta, half_life_days)
    half_life = ln(2) / theta

    TODO:DERIVE — window 30d default; validate against Granger causality test
    on Engine B closed trade pairs.
    """
    prices = df["close"].tail(window).values
    if len(prices) < 10:
        return 0.0, float("inf")

    log_p   = np.log(prices)
    delta   = np.diff(log_p)   # log_price[t] - log_price[t-1]
    lag     = log_p[:-1]       # log_price[t-1]

    # OLS: delta = a + b × lag
    try:
        b, a = np.polyfit(lag, delta, 1)
        theta = -b  # b = -theta (negative for mean-reverting series)
        if theta <= 0:
            return 0.0, float("inf")
        half_life = math.log(2) / theta
        return round(theta, 6), round(half_life, 2)
    except Exception:
        return 0.0, float("inf")


def compute_engine_score(avwap_dist: float, stoch_div: bool,
                          vol_climax: float, thresholds: dict) -> float:
    """
    Composite engine score for Engine B ∈ [0, 1].

    Components:
      1. AVWAP distance: how far below AVWAP (more negative = higher score) → [0,1]
      2. Stochastic divergence: binary 0 or 1
      3. Volume climax: vol ratio clipped [1.5, 3.0] → [0,1]

    RF-14: loads IC-calibrated weights from sub_engine_params.json via
    ares_weights.load_signal_weights("B"). Falls back to equal (1/3 each) during bootstrap.
    """
    from ares_weights import load_signal_weights

    # AVWAP distance: gate is <= -1.0, strong signal at <= -2.5
    avwap_score = min(1.0, max(0.0, (-avwap_dist - 1.0) / 1.5))

    # Stochastic divergence: binary
    stoch_score = 1.0 if stoch_div else 0.0

    # Volume climax: gate is 1.5, strong at 3.0
    vol_score = min(1.0, max(0.0, (vol_climax - 1.5) / 1.5))

    weights = load_signal_weights("B")
    w_avwap = weights.get("b_avwap_distance",   1.0 / 3)
    w_stoch = weights.get("b_stoch_divergence", 1.0 / 3)
    w_vol   = weights.get("b_volume_climax",    1.0 / 3)

    score = w_avwap * avwap_score + w_stoch * stoch_score + w_vol * vol_score
    return round(float(min(1.0, max(0.0, score))), 4)


# ── OU-based hold target ──────────────────────────────────────────────────────

def compute_hold_target(theta: float, half_life: float) -> int:
    """
    hold_target = min(int(half_life × 1.5), 20)

    Rationale: OU half-life × 1.5 gives time for 75% of the reversion to occur.
    Capped at 20 days — beyond 20 days, MR thesis is likely stale.
    If theta is near zero (weak mean reversion), defaults to 10-day hold.

    TODO:DERIVE — 1.5 multiplier from optimal stopping analysis on Engine B trades.
    """
    if theta <= 0 or half_life > 30:
        return 10  # default: weak OU, use conservative hold
    return min(int(half_life * 1.5), 20)


# ── Sizing ────────────────────────────────────────────────────────────────────

def compute_position_size(equity: float, engine_score: float,
                           engine_capital_remaining: float,
                           buying_power: float) -> int:
    """
    Engine B position sizing. RF-13: min_pct floor disabled during bootstrap.
    """
    from ares_config import KELLY_APPLY_FLOOR_IN_BOOTSTRAP
    from ares_weights import is_calibrated
    min_pct, max_pct = KELLY_ENGINE_CLIPS["B"]

    score_min = 0.60
    kelly_scale = 0.5 + max(0.0, (engine_score - score_min) / (1.0 - score_min)) * 0.5
    kelly_scale = round(min(1.0, max(0.5, kelly_scale)), 4)

    kelly_dollar = equity * KELLY_BOOTSTRAP_FRACTION * KELLY_SHADOW_MULTIPLIER * kelly_scale

    # RF-13: only apply floor post-calibration
    if KELLY_APPLY_FLOOR_IN_BOOTSTRAP or is_calibrated("B"):
        kelly_dollar = max(equity * min_pct, kelly_dollar)
    kelly_dollar = min(equity * max_pct, kelly_dollar)

    position_dollar = min(kelly_dollar, engine_capital_remaining, buying_power * 0.95)
    return position_dollar, kelly_scale


# ── Macro gate ────────────────────────────────────────────────────────────────

def get_macro_multiplier() -> Tuple[float, str]:
    """
    Load macro_context.json and return Engine B's regime multiplier.
    Engine B: RISK_ON=0.5, NEUTRAL=1.0, RISK_OFF=1.0, CRISIS=0.0

    RF-16: uses probability BLEND from regime_probs if available, not cliff-switched argmax.
    Falls back to discrete label only when probs are absent.
    """
    macro  = _load_json(MACRO_FILE, {})
    regime = macro.get("macro_regime", "NEUTRAL")
    probs  = macro.get("regime_probs")
    gates  = ENGINE_REGIME_GATE[ENGINE_ID]

    if probs:
        multiplier = (
            probs.get("RISK_ON",  0) * gates.get("RISK_ON",  0.5) +
            probs.get("NEUTRAL",  0) * gates.get("NEUTRAL",  1.0) +
            probs.get("RISK_OFF", 0) * gates.get("RISK_OFF", 1.0) +
            probs.get("CRISIS",   0) * gates.get("CRISIS",   0.0)
        )
    else:
        multiplier = gates.get(regime, 0.0)

    return round(multiplier, 4), regime


# ── Shadow logging ────────────────────────────────────────────────────────────

def log_shadow(symbol: str, entry_price: float, engine_score: float,
               signals: dict, macro_regime: str, would_trade: bool,
               skip_reason: Optional[str] = None):
    """Append one shadow classification record to shadow_classifications.json."""
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})

    record = {
        "shadow_id":         f"B-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
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
    shadow_data["classifications"].append(record)
    _save_json_atomic(SHADOW_FILE, shadow_data)


def log_unconditioned_shadow(symbol: str, entry_price: float, engine_score: float,
                             signals: dict, macro_regime: str, owned_by: Optional[str]):
    """RF-10 mitigation: log symbols that pass signal gates regardless of ownership."""
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})
    record = {
        "shadow_id":              f"B-UNC-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
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
    """
    Full Engine B scan.

    1. Load universe, macro regime, ledger, account state.
    2. For each symbol: ownership check → bar fetch → signal compute → gate check.
    3. In shadow mode: log all passing candidates, no orders.
    4. In live mode: submit orders up to MAX_POSITIONS_PER_ENGINE["B"].

    Returns list of classification dicts for this scan.
    """
    status = ENGINE_STATUS.get(ENGINE_ID, "off")
    if status == "off":
        logger.info("Engine B is OFF — skipping scan.")
        return []

    macro_mult, macro_regime = get_macro_multiplier()
    if macro_mult == 0.0:
        logger.info("Engine B gated out by macro regime: %s (multiplier=0)", macro_regime)
        return []

    logger.info("Engine B scan | regime=%s | mult=%.2f | mode=%s", macro_regime, macro_mult, status)

    # Load universe
    universe_data = _load_json(UNIVERSE_FILE, {})
    symbols = list(universe_data.get("symbols", []))
    if not symbols:
        logger.warning("Universe file empty or missing — aborting scan.")
        return []

    logger.info("Universe size: %d symbols", len(symbols))

    # Load ledger for ownership and capital checks
    ledger = Ledger()
    ownership = ledger._load_ownership()

    # Account state
    try:
        acct = get_account()
        equity        = float(acct.get("equity", TOTAL_EQUITY))
        buying_power  = float(acct.get("buying_power", 0))
    except Exception as e:
        if status == "live":
            logger.error("Account fetch failed in LIVE mode: %s — aborting scan (no fabricated $)", e)
            return []
        logger.warning("Account fetch failed (shadow mode): %s — using config equity", e)
        equity       = TOTAL_EQUITY
        buying_power = equity * 0.5

    engine_weight    = INITIAL_ENGINE_WEIGHTS[ENGINE_ID]
    engine_ceiling   = equity * engine_weight
    engine_deployed  = ledger.get_engine_capital_deployed(ENGINE_ID)
    engine_remaining = engine_ceiling - engine_deployed
    open_positions   = len(ledger.get_positions(ENGINE_ID))
    max_positions    = MAX_POSITIONS_PER_ENGINE[ENGINE_ID]

    logger.info("Engine B capital: ceiling=$%.0f deployed=$%.0f remaining=$%.0f  "
                "positions=%d/%d",
                engine_ceiling, engine_deployed, engine_remaining,
                open_positions, max_positions)

    thresholds  = CLASSIFICATION_THRESHOLDS[ENGINE_ID]
    stop_params = ENGINE_STOP_PARAMS[ENGINE_ID]
    classified  = []
    entries_this_scan = 0

    # Fetch bars in one batch
    logger.info("Fetching bars for %d symbols...", len(symbols))
    bars_map = fetch_bars(symbols)
    logger.info("Bars returned for %d symbols", len(bars_map))

    for symbol, df in bars_map.items():
        # ── GATE 1: Ownership ─────────────────────────────────────────────────
        if symbol in ownership:
            continue

        if len(df) < 30:
            continue

        price = float(df["close"].iloc[-1])
        if price <= 0:
            continue

        atr = _atr(df)
        if atr <= 0:
            continue

        # ── Signals ───────────────────────────────────────────────────────────
        avwap_dist         = b_avwap_distance(df, atr)
        stoch_div, stoch_k = b_stoch_divergence(df)
        vol_climax         = b_volume_climax(df)
        theta, half_life   = estimate_ou_theta(df)
        hold_target        = compute_hold_target(theta, half_life)

        signals = {
            "b_avwap_distance":   avwap_dist,
            "b_stoch_divergence": stoch_div,
            "b_stoch_k":          stoch_k,
            "b_volume_climax":    vol_climax,
            "b_ou_theta":         theta,
            "b_ou_half_life":     half_life,
            "b_hold_target_days": hold_target,
            "atr":                round(atr, 4),
            "price":              round(price, 4),
        }

        # ── GATE 2: Signal thresholds ─────────────────────────────────────────
        avwap_gate = thresholds.get("avwap_gate_atr", -1.0)
        vol_gate   = 1.5   # TODO:DERIVE from shadow data
        score_min  = thresholds.get("engine_score_min", 0.60)

        if avwap_dist > avwap_gate:
            continue   # not far enough below AVWAP
        if not stoch_div:
            continue   # no oversold + divergence
        if vol_climax < vol_gate:
            continue   # no capitulation volume

        engine_score = compute_engine_score(avwap_dist, stoch_div, vol_climax, thresholds)

        if engine_score < score_min:
            continue

        # ── GATE 3: Capacity ──────────────────────────────────────────────────
        would_trade   = True
        skip_reason   = None

        if open_positions >= max_positions:
            would_trade = False
            skip_reason = f"at_position_limit ({open_positions}/{max_positions})"

        if engine_remaining < equity * 0.02:
            would_trade = False
            skip_reason = f"engine_capital_exhausted (remaining=${engine_remaining:.0f})"

        # ── Shadow log (always) ───────────────────────────────────────────────
        log_shadow(symbol, price, engine_score, signals, macro_regime,
                   would_trade and (status == "shadow"),
                   skip_reason=skip_reason)

        classified.append({
            "symbol":        symbol,
            "engine_score":  engine_score,
            "signals":       signals,
            "would_trade":   would_trade,
            "hold_target":   hold_target,
        })

        logger.info(
            "CLASSIFY B | %s | score=%.3f | avwap=%.2f ATR | stoch_k=%.1f | "
            "vol_climax=%.2f | OU_hl=%.1fd | hold=%dd | %s",
            symbol, engine_score, avwap_dist, stoch_k, vol_climax,
            half_life, hold_target, "WOULD_TRADE" if would_trade else f"SKIP:{skip_reason}"
        )

        # ── Live entry ────────────────────────────────────────────────────────
        if status == "live" and would_trade and not dry_run:
            if entries_this_scan >= max_positions:
                break

            position_dollar, kelly_scale = compute_position_size(
                equity, engine_score, engine_remaining, buying_power
            )

            # Stop price: ATR-based (vol-interpolated)
            # TODO:DERIVE — stop_mult from EVT tail on Engine B returns
            stop_mult = stop_params.get("stop_mult_low_vol", 2.0)
            stop_price = round(price - stop_mult * atr, 4)

            qty = max(1, int(position_dollar / price))
            limit_price = round(price * 1.001, 2)  # 10bps above last close

            client_order_id = f"B_entry_{symbol}_{datetime.now():%Y%m%d%H%M%S}"

            # Check ownership once more (race condition guard)
            if ledger.is_symbol_owned(symbol):
                logger.info("SKIP %s — claimed between check and order", symbol)
                continue

            order = submit_order(symbol, qty, client_order_id, limit_price, dry_run=dry_run)
            if order and order.get("id"):
                entry_date = datetime.now().strftime("%Y-%m-%d")
                ledger.record_entry(
                    engine_id    = ENGINE_ID,
                    symbol       = symbol,
                    shares       = qty,
                    entry_price  = price,
                    date         = entry_date,
                    metadata     = {
                        "engine_score":     engine_score,
                        "kelly_scale":      kelly_scale,
                        "stop_price":       stop_price,
                        "hold_target_days": hold_target,
                        "macro_regime":     macro_regime,
                        "signals_at_entry": signals,
                    }
                )
                logger.info(
                    "ENTRY B | %s | qty=%d | price=%.2f | stop=%.2f | "
                    "hold_target=%dd | score=%.3f",
                    symbol, qty, price, stop_price, hold_target, engine_score
                )
                entries_this_scan += 1
                engine_remaining -= qty * price
                open_positions   += 1

    shadow_count = count_shadow_entries()
    logger.info(
        "Engine B scan complete | classified=%d | entries=%d | shadow_total=%d/%d",
        len(classified), entries_this_scan, shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE
    )

    # ── RF-10: Unconditioned pass — score owned symbols for IC bias correction ─
    unc_logged = 0
    avwap_gate = thresholds.get("avwap_gate_atr", -1.0)
    vol_gate   = 1.5
    _score_min = thresholds.get("engine_score_min", 0.60)
    for symbol, df in bars_map.items():
        if symbol not in ownership:
            continue
        if len(df) < 30:
            continue
        price = float(df["close"].iloc[-1])
        if price <= 0:
            continue
        atr = _atr(df)
        if atr <= 0:
            continue
        avwap_dist         = b_avwap_distance(df, atr)
        stoch_div, stoch_k = b_stoch_divergence(df)
        vol_climax         = b_volume_climax(df)
        theta, half_life   = estimate_ou_theta(df)
        hold_target        = compute_hold_target(theta, half_life)
        if avwap_dist > avwap_gate or not stoch_div or vol_climax < vol_gate:
            continue
        engine_score_unc = compute_engine_score(avwap_dist, stoch_div, vol_climax, thresholds)
        if engine_score_unc < _score_min:
            continue
        sigs_unc = {
            "b_avwap_distance": avwap_dist, "b_stoch_divergence": stoch_div,
            "b_volume_climax": vol_climax, "b_ou_theta": theta,
            "atr": round(atr, 4), "price": round(price, 4),
        }
        log_unconditioned_shadow(symbol, price, engine_score_unc, sigs_unc,
                                 macro_regime, owned_by=ownership[symbol])
        unc_logged += 1
    if unc_logged:
        logger.info("RF-10: logged %d unconditioned Engine B candidates (owned by others)", unc_logged)

    if status == "shadow" and shadow_count >= SHADOW_MIN_ENTRIES_FOR_LIVE:
        logger.info(
            "ENGINE B SHADOW GATE MET (%d/%d) — update ENGINE_STATUS['B'] = 'live' to enable.",
            shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE
        )

    return classified


# ── Status ────────────────────────────────────────────────────────────────────

def print_status():
    ledger       = Ledger()
    positions    = ledger.get_positions(ENGINE_ID)
    shadow_count = count_shadow_entries()
    status       = ENGINE_STATUS.get(ENGINE_ID, "off")

    print(f"\n{'='*60}")
    print(f"  ENGINE B STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Mode:            {status.upper()}")
    print(f"  Shadow entries:  {shadow_count}/{SHADOW_MIN_ENTRIES_FOR_LIVE}")
    print(f"  Open positions:  {len(positions)}")
    for p in positions:
        print(f"    {p['symbol']:8s}  {p['shares']} shares @ ${p['entry_price']:.2f}"
              f"  ({p['entry_date']})")
    print(f"{'='*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Engine B — Mean Reversion")
    parser.add_argument("--scan",   action="store_true", help="Run full scan")
    parser.add_argument("--dry",    action="store_true", help="Dry run — no orders")
    parser.add_argument("--status", action="store_true", help="Show current positions")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.scan:
        results = scan(dry_run=args.dry)
        print(f"\n[Engine B] Scan complete — {len(results)} classification(s)")
    else:
        parser.print_help()
