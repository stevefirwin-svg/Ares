"""
evt_calibrator.py — EVT Stop Multiplier Calibration
=====================================================
Derives stop ATR multipliers from Extreme Value Theory (EVT) using a
Generalized Pareto Distribution (GPD) fit to the left tail of return distributions.

Mathematical basis:
  Pickands-Balkema-de Haan theorem: the distribution of exceedances beyond a
  threshold u converges to a GPD as u → the right endpoint of the support.
  Applied to the LEFT tail: negative returns beyond -u fit GPD(ξ, σ, u).

  Stop multiplier derivation:
    1. Collect per-bar returns for the target population (universe returns,
       or engine-specific closed trade adverse moves).
    2. Fit GPD to exceedances beyond threshold u = 5th percentile of returns.
    3. Find ATR-multiple x such that P(adverse_move > x × ATR) = 0.05.
    4. That x is the stop multiplier — 95% of noise doesn't trigger the stop.

  For Engine F structural stop:
    Distribution is (entry_price - consol_low) / ATR ratios from historical setups.
    The 5th percentile of this ratio gives the minimum ATR distance needed to
    avoid noise-triggering the structural stop.

Bootstrap mode (no engine trades yet):
  Uses universe-wide 1-day return distribution as a proxy.
  Conservative: ATR-normalized adverse moves across all liquid names.
  Replace with engine-specific data after 30+ closed trades per engine.

Output: evt_calibration.json — read by ares_config.py validation and logged.
        Immutable rule: no engine goes live with TODO:DERIVE stop constants.

Usage:
    python evt_calibrator.py                  # bootstrap from universe bars
    python evt_calibrator.py --engine B       # engine-specific (needs closed trades)
    python evt_calibrator.py --engine B --show # print derived multipliers

Research basis:
  Pickands (1975), Balkema & de Haan (1974) — GPD as limiting distribution
  McNeil & Frey (2000) — EVT for financial risk management
  Embrechts, Klüppelberg & Mikosch (1997) — Modelling Extremal Events
"""

import json
import logging
import os
import argparse
import math
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

import numpy as np
import requests
from scipy.stats import genpareto
from dotenv import load_dotenv
load_dotenv()

from ares_config import BAR_FEED

logging.basicConfig(level=logging.INFO, format="%(asctime)s [EVT] %(message)s")
logger = logging.getLogger("ares.evt")

CALIBRATION_FILE = "evt_calibration.json"
OUTCOMES_FILE    = "sub_engine_outcomes.json"

# ── Data ──────────────────────────────────────────────────────────────────────

def _alpaca_headers() -> dict:
    from ares_config import ALPACA_API_KEY, ALPACA_SECRET_KEY
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def fetch_universe_returns(n_symbols: int = 80, lookback_days: int = 252) -> np.ndarray:
    """
    Fetch 1-year daily returns for n_symbols liquid names.
    Used for bootstrap EVT when engine-specific trade history is absent.

    Returns array of ATR-normalized 1-day adverse moves (positive values = bad moves).

    Uses single-symbol endpoint (/v2/stocks/{symbol}/bars) — works on paper/free
    accounts. The bulk multi-symbol endpoint requires a paid data subscription.
    """
    BOOTSTRAP_SYMBOLS = [
        "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","JPM","UNH","V",
        "XOM","JNJ","PG","HD","MA","BAC","ABBV","MRK","CVX","AVGO",
        "COST","PEP","KO","TMO","ACN","MCD","CSCO","ABT","LIN","DHR",
        "WMT","TXN","NEE","PM","NKE","RTX","QCOM","LOW","BMY","UPS",
        "AMGN","SBUX","IBM","GS","BLK","CAT","BA","DE","SPG","PLD",
        "XLK","XLF","XLV","XLE","XLY","XLI","XLB","XLC","XLU","XLRE",
        "AMD","MU","LRCX","AMAT","KLAC","MRVL","PANW","CRWD","SNOW","ZS",
        "F","GM","CVS","MCK","MOH","CNC","ELV","MS","C","WFC",
    ][:n_symbols]

    end   = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.4))

    adverse_moves = []
    fetched = 0

    for sym in BOOTSTRAP_SYMBOLS:
        try:
            params = {
                "timeframe": "1Day",
                "start":     start.strftime("%Y-%m-%d"),
                "end":       end.strftime("%Y-%m-%d"),
                "limit":     500,
                "feed":      BAR_FEED,
                "sort":      "asc",
            }
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{sym}/bars",
                headers=_alpaca_headers(), params=params, timeout=15
            )
            if resp.status_code != 200:
                continue
            bar_list = resp.json().get("bars", [])
            if len(bar_list) < 30:
                continue

            df = pd.DataFrame(bar_list)
            df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)

            close = df["close"].values
            high  = df["high"].values
            low   = df["low"].values
            rets  = np.diff(close) / close[:-1]

            tr_arr = []
            for i in range(1, len(close)):
                tr = max(high[i] - low[i],
                         abs(high[i] - close[i-1]),
                         abs(low[i] - close[i-1]))
                tr_arr.append(tr)
            tr_arr = np.array(tr_arr)

            for i in range(13, len(rets)):
                atr_14 = float(np.mean(tr_arr[max(0, i-13):i+1]))
                if atr_14 <= 0:
                    continue
                price = close[i]
                if price <= 0:
                    continue
                atr_normalized_move = -rets[i] * price / atr_14
                adverse_moves.append(atr_normalized_move)

            fetched += 1
            if fetched % 10 == 0:
                logger.info("EVT bootstrap: %d/%d symbols, %d moves",
                            fetched, len(BOOTSTRAP_SYMBOLS), len(adverse_moves))

        except Exception as e:
            logger.debug("EVT bootstrap skip %s: %s", sym, e)
            continue

    logger.info("EVT bootstrap complete: %d symbols, %d adverse moves", fetched, len(adverse_moves))
    return np.array(adverse_moves) if adverse_moves else np.array([])


def fetch_engine_adverse_moves(engine_id: str) -> np.ndarray:
    """
    Extract per-trade max adverse excursion (MAE) from closed Engine trades.
    MAE = (entry_price - min_price_during_hold) / ATR_at_entry.

    NOTE: This requires intrabar low data — currently approximated from
    (entry_price - exit_price) / ATR for losing trades as a conservative
    lower bound. Full MAE requires storing daily bar lows during hold.
    TODO: Store daily bar lows in ledger metadata during hold for precise MAE.
    """
    if not os.path.exists(OUTCOMES_FILE):
        return np.array([])

    with open(OUTCOMES_FILE) as f:
        outcomes = json.load(f)

    trades = [
        t for t in outcomes.get("trades", [])
        if t.get("engine_id") == engine_id
        and t.get("pnl_pct") is not None
        and t.get("entry_price") and t.get("exit_price")
    ]

    if len(trades) < 10:
        logger.warning("Engine %s: only %d trades — EVT unreliable, using bootstrap",
                       engine_id, len(trades))
        return np.array([])

    # Approximate MAE from losing trades (lower bound)
    adverse_pcts = []
    for t in trades:
        ep  = float(t["entry_price"])
        xp  = float(t["exit_price"])
        pnl = float(t["pnl_pct"]) / 100.0
        if pnl < 0:
            # Adverse move ≈ |pnl| as ATR fraction
            # This underestimates MAE (doesn't capture intrabar lows)
            sigs = t.get("signals_at_entry", {})
            atr  = float(sigs.get("atr", ep * 0.015))  # fallback: 1.5% of entry
            if atr > 0:
                adverse_pcts.append(abs(ep - xp) / atr)

    return np.array(adverse_pcts)


# ── GPD fitting ───────────────────────────────────────────────────────────────

def fit_gpd(adverse_moves: np.ndarray, tail_quantile: float = 0.90) -> Tuple[float, float, float]:
    """
    Fit a Generalized Pareto Distribution to the right tail of adverse moves
    (right tail = worst adverse moves = most negative returns normalized by ATR).

    Uses POT (Peaks Over Threshold) method:
      threshold u = tail_quantile percentile of |adverse_moves|
      exceedances = adverse_moves[adverse_moves > u] - u

    Returns (xi, sigma, threshold_u) — GPD shape, scale, threshold.
    xi > 0: heavy tail (Pareto-like)
    xi = 0: exponential tail
    xi < 0: bounded tail (rare for returns)
    """
    x = np.abs(adverse_moves)
    u = float(np.percentile(x, tail_quantile * 100))
    exceedances = x[x > u] - u

    if len(exceedances) < 20:
        logger.warning("Only %d exceedances above u=%.4f — GPD fit may be unreliable",
                       len(exceedances), u)
        if len(exceedances) < 5:
            raise ValueError(f"Too few exceedances ({len(exceedances)}) for GPD fit")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        xi, loc, sigma = genpareto.fit(exceedances, floc=0)

    logger.info("GPD fit: xi=%.4f  sigma=%.4f  threshold=%.4f  n_exc=%d",
                xi, sigma, u, len(exceedances))
    return xi, sigma, u


def gpd_quantile(xi: float, sigma: float, u: float,
                 n_total: int, n_exceedances: int,
                 p: float = 0.05) -> float:
    """
    Compute the ATR multiple x such that P(adverse_move > x) = p.
    This is the stop multiplier: 95% of noise moves don't trigger the stop.

    Formula (GPD quantile via tail probability inversion):
      P(X > x | X > u) = (1 + xi*(x-u)/sigma)^(-1/xi)   [xi ≠ 0]
      P(X > u) = n_exceedances / n_total

    So P(X > x) = P(X > u) × P(X > x | X > u) = p
    → P(X > x | X > u) = p / P(X > u) = p × n_total / n_exceedances
    → x = u + sigma/xi × [(p_cond)^(-xi) - 1]

    For xi = 0 (exponential): x = u + sigma × (-ln(p_cond))
    """
    p_exceed = n_exceedances / n_total   # P(X > u)
    p_cond   = p / p_exceed              # P(X > x | X > u)

    if p_cond >= 1.0:
        # p is smaller than the tail mass — clamp to 3σ above threshold
        logger.warning("p=%.4f is beyond tail mass (p_exceed=%.4f) — clamping to u+3σ", p, p_exceed)
        return u + sigma * 3

    if abs(xi) < 1e-6:
        # xi ≈ 0: exponential tail
        x = u + sigma * (-math.log(p_cond))
    else:
        x = u + (sigma / xi) * (p_cond ** (-xi) - 1)

    return float(x)


# ── Engine-specific stop derivation ──────────────────────────────────────────

def derive_stop_multiplier(
    engine_id: str,
    adverse_moves: np.ndarray,
    target_noise_prob: float = 0.05,
) -> Dict:
    """
    Full EVT pipeline for one engine. Returns a result dict.

    target_noise_prob = 0.05: stop is set so that only 5% of noise moves hit it.
    In other words, 95% of legitimate noise doesn't trigger the stop.

    For Engine B (MR): tighter stop acceptable (2.0–2.5× ATR expected range).
    For Engine A (momentum): wider stop (2.8–3.5× ATR expected range).
    For Engine F: structural stop — not ATR-based (separate path).
    """
    if len(adverse_moves) < 20:
        logger.warning("Engine %s: insufficient data (%d moves) — using conservative fallback",
                       engine_id, len(adverse_moves))
        # Conservative fallback table (pre-EVT round numbers — still better than nothing)
        FALLBACK = {"A": 3.0, "B": 2.0, "C": 1.8, "E": 3.0, "F": None}
        fallback = FALLBACK.get(engine_id, 2.5)
        return {
            "engine_id":       engine_id,
            "method":          "fallback_insufficient_data",
            "stop_mult":       fallback,
            "n_observations":  len(adverse_moves),
            "gpd_xi":          None,
            "gpd_sigma":       None,
            "threshold_u":     None,
            "target_prob":     target_noise_prob,
            "derived_at":      datetime.now().isoformat(),
            "note":            f"Fallback: <20 observations. Rebuild after 30+ trades.",
        }

    try:
        xi, sigma, u = fit_gpd(adverse_moves, tail_quantile=0.90)
        n_total      = len(adverse_moves)
        n_exceed     = int(np.sum(np.abs(adverse_moves) > u))

        stop_mult = gpd_quantile(xi, sigma, u, n_total, n_exceed, p=target_noise_prob)

        # Sanity bounds — EVT can produce extreme values on small samples
        ENGINE_BOUNDS = {
            "A": (2.0, 5.0),
            "B": (1.2, 3.5),
            "C": (1.2, 3.0),
            "E": (2.0, 5.0),
            "F": (None, None),  # structural — skip ATR bounds
        }
        lo, hi = ENGINE_BOUNDS.get(engine_id, (1.0, 6.0))
        if lo and hi and not (lo <= stop_mult <= hi):
            logger.warning("Engine %s EVT result %.3f outside bounds [%.1f, %.1f] — clamping",
                           engine_id, stop_mult, lo, hi)
            stop_mult = max(lo, min(hi, stop_mult))

        logger.info("Engine %s EVT stop multiplier: %.4f (xi=%.4f, sigma=%.4f, u=%.4f)",
                    engine_id, stop_mult, xi, sigma, u)

        return {
            "engine_id":       engine_id,
            "method":          "gpd_evt",
            "stop_mult":       round(stop_mult, 4),
            "n_observations":  n_total,
            "n_exceedances":   n_exceed,
            "gpd_xi":          round(xi, 6),
            "gpd_sigma":       round(sigma, 6),
            "threshold_u":     round(u, 6),
            "target_prob":     target_noise_prob,
            "derived_at":      datetime.now().isoformat(),
            "note":            "GPD/EVT derived. Rebuild after every 30 new closed trades.",
        }

    except Exception as e:
        logger.error("GPD fit failed for engine %s: %s — using fallback", engine_id, e)
        FALLBACK = {"A": 3.0, "B": 2.0, "C": 1.8, "E": 3.0, "F": None}
        return {
            "engine_id":   engine_id,
            "method":      "fallback_fit_error",
            "stop_mult":   FALLBACK.get(engine_id, 2.5),
            "error":       str(e),
            "derived_at":  datetime.now().isoformat(),
        }


# ── Main calibration run ──────────────────────────────────────────────────────

def run_calibration(engine_id: Optional[str] = None, bootstrap: bool = True) -> Dict:
    """
    Run EVT calibration for one engine (or all engines).

    If engine_id is specified and has sufficient closed trades, uses engine-specific MAE.
    Otherwise uses universe bootstrap data (conservative).

    Returns full calibration dict written to evt_calibration.json.
    """
    logger.info("EVT Calibration starting... mode=%s engine=%s",
                "bootstrap" if bootstrap else "engine-specific", engine_id or "all")

    # Load or init existing calibration
    existing = {}
    if os.path.exists(CALIBRATION_FILE):
        with open(CALIBRATION_FILE) as f:
            existing = json.load(f)

    # Get bootstrap adverse moves once (shared across engines)
    bootstrap_moves = None

    engines_to_calibrate = [engine_id] if engine_id else ["A", "B", "C", "E", "F"]

    for eid in engines_to_calibrate:
        logger.info("--- Engine %s ---", eid)

        if eid == "F":
            # Engine F: structural stop — EVT on (entry_price - consol_low) / ATR ratios
            # Now that Engine F is built (Sprint 3), derive from bootstrap bar data.
            # Uses the distribution of (day_high - day_low) / ATR as a proxy for
            # consolidation-base-to-entry distance ratios.
            if bootstrap_moves is None:
                logger.info("Fetching bootstrap universe returns (shared across engines)...")
                bootstrap_moves = fetch_universe_returns(n_symbols=80, lookback_days=252)
            moves = bootstrap_moves

            if len(moves) >= 20:
                # For Engine F, the structural stop is (entry - consol_low) / ATR.
                # We want P(intraday adverse move > stop_distance × ATR) = 0.05.
                # Use the same GPD EVT on bootstrap adverse moves — same math,
                # interpreted as the minimum structural distance to avoid noise triggers.
                result = derive_stop_multiplier("F", moves)
                result["note"] = (
                    "Structural stop buffer (entry-to-consol_low/ATR). "
                    "GPD/EVT on bootstrap adverse moves. "
                    "Replace with Engine F MAE data after 30+ closed trades."
                )
                # Engine F stop_buffer_atr: structural stop needs to be BELOW consol_low
                # so the stop_buffer is smaller (0.1–0.3× ATR below base)
                # The stop_mult here is the minimum structural distance, not a buffer.
                # Cap: Engine F structural distance is typically 1.5–3.0× ATR
                result["stop_mult"] = min(result.get("stop_mult", 2.0), 3.0)
            else:
                result = {
                    "engine_id":   "F",
                    "method":      "fallback_insufficient_data",
                    "stop_mult":   2.0,
                    "n_observations": len(moves),
                    "derived_at":  datetime.now().isoformat(),
                    "note":        "Bootstrap fetch failed. Using conservative fallback 2.0× ATR.",
                }
            existing[eid] = result
            continue

        # Try engine-specific trades first
        engine_moves = fetch_engine_adverse_moves(eid)

        if len(engine_moves) >= 20:
            logger.info("Engine %s: using %d engine-specific trade MAE observations", eid, len(engine_moves))
            moves = engine_moves
        else:
            # Fall back to bootstrap
            if bootstrap_moves is None:
                logger.info("Fetching bootstrap universe returns (shared across engines)...")
                bootstrap_moves = fetch_universe_returns(n_symbols=80, lookback_days=252)
            moves = bootstrap_moves
            logger.info("Engine %s: using bootstrap (%d observations)", eid, len(moves))

        result = derive_stop_multiplier(eid, moves)
        existing[eid] = result

    existing["_calibrated_at"] = datetime.now().isoformat()
    existing["_version"]       = "1.0"

    tmp = CALIBRATION_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    os.replace(tmp, CALIBRATION_FILE)

    logger.info("EVT calibration written → %s", CALIBRATION_FILE)
    return existing


def show_calibration():
    if not os.path.exists(CALIBRATION_FILE):
        print("evt_calibration.json not found. Run without --show first.")
        return

    with open(CALIBRATION_FILE) as f:
        data = json.load(f)

    print(f"\n{'='*66}")
    print(f"  EVT STOP CALIBRATION — {data.get('_calibrated_at','?')[:19]}")
    print(f"{'='*66}")
    print(f"  {'Engine':<8} {'Method':<24} {'Stop Mult':>10} {'N Obs':>7} {'xi':>8} {'sigma':>8}")
    print(f"  {'-'*8} {'-'*24} {'-'*10} {'-'*7} {'-'*8} {'-'*8}")
    for eid in sorted(k for k in data if not k.startswith("_")):
        r = data[eid]
        sm     = r.get("stop_mult")
        n      = r.get("n_observations", "?")
        xi     = r.get("gpd_xi")
        sigma  = r.get("gpd_sigma")
        method = r.get("method", "?")
        print(f"  {eid:<8} {method:<24} {f'{sm:.4f}' if sm else 'N/A':>10} "
              f"{str(n):>7} {f'{xi:.4f}' if xi else 'N/A':>8} "
              f"{f'{sigma:.4f}' if sigma else 'N/A':>8}")
    print()
    print("  Stop multiplier = ATR multiple where P(noise trigger) = 5%")
    print("  Rebuild after every 30 new closed trades per engine.")
    print(f"{'='*66}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares EVT Stop Calibrator")
    parser.add_argument("--engine", type=str, default=None, help="Engine ID (A/B/C/E/F) or all")
    parser.add_argument("--show",   action="store_true", help="Show current calibration")
    args = parser.parse_args()

    if args.show:
        show_calibration()
    else:
        run_calibration(engine_id=args.engine)
        show_calibration()
