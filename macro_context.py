"""
macro_context.py — Ares Macro Context Engine
=============================================
Pulls daily macro signals and classifies the market regime.
Writes macro_context.json before market open every day.

NO FRED data. Ares uses only market-priced signals:
  - VIX (implied volatility)
  - SPY trend (20d return, MA relationship)
  - Credit spread via HYG/LQD price ratio (market-priced HY-IG proxy)
  - Sector breadth (% of sector ETFs above 50/150/200 MA)

Regime labels: RISK_ON / NEUTRAL / RISK_OFF / CRISIS

Run:
    python macro_context.py            # fetch and write macro_context.json
    python macro_context.py --summary  # print current macro context

Scheduled via Task Scheduler at 8:00 AM ET (before engine scans at 9:35 AM).
"""

import os
import json
import argparse
import numpy as np
import yfinance as yf
from datetime import datetime, timezone

MACRO_CONTEXT_PATH = "macro_context.json"

SECTOR_ETFS = [
    "XLK", "XLF", "XLV", "XLE", "XLY",
    "XLI", "XLB", "XLC", "XLU", "XLRE", "XLP"
]

# ── Signal pullers ────────────────────────────────────────────────────────────

def get_vix() -> dict:
    """VIX level and regime classification."""
    try:
        hist = yf.Ticker("^VIX").history(period="5d")
        if hist.empty:
            return {"value": None, "regime": "UNKNOWN"}
        level = round(float(hist["Close"].iloc[-1]), 2)
        if level >= 35:
            regime = "CRISIS"
        elif level >= 25:
            regime = "ELEVATED"
        elif level >= 18:
            regime = "NEUTRAL"
        else:
            regime = "CALM"
        return {"value": level, "regime": regime}
    except Exception as e:
        print(f"  [VIX] WARNING: {e}")
        return {"value": None, "regime": "UNKNOWN"}


def get_spy_trend() -> dict:
    """SPY 20-day trend and 50/200 MA relationship."""
    try:
        hist = yf.Ticker("SPY").history(period="1y")
        if len(hist) < 50:
            return {"trend_20d": None, "above_50ma": None, "above_200ma": None, "regime": "UNKNOWN"}
        close     = hist["Close"]
        price     = float(close.iloc[-1])
        ma50      = float(close.rolling(50).mean().iloc[-1])
        ma200     = float(close.rolling(200).mean().iloc[-1])
        trend_20d = round((price / float(close.iloc[-20]) - 1) * 100, 2)
        above_50  = price > ma50
        above_200 = price > ma200

        if above_200 and above_50 and trend_20d > 2:
            regime = "BULLISH"
        elif above_200 and trend_20d > -2:
            regime = "NEUTRAL"
        elif not above_200 and trend_20d < -2:
            regime = "BEARISH"
        else:
            regime = "MIXED"

        return {
            "price":       round(price, 2),
            "ma50":        round(ma50, 2),
            "ma200":       round(ma200, 2),
            "trend_20d":   trend_20d,
            "above_50ma":  above_50,
            "above_200ma": above_200,
            "regime":      regime,
        }
    except Exception as e:
        print(f"  [SPY] WARNING: {e}")
        return {"trend_20d": None, "above_50ma": None, "above_200ma": None, "regime": "UNKNOWN"}


def get_credit_spread() -> dict:
    """
    Market-priced HY-IG credit spread proxy using HYG / LQD price ratio.
    No FRED required. Ratio declining = spread widening = credit stress.

    Methodology:
      - Pull 60 days of HYG and LQD daily closes
      - Compute ratio = HYG / LQD, normalized to 60d mean = 1.0
      - ratio < 0.97 → spread widening (stress signal)
      - ratio < 0.94 → significant stress

    TODO:DERIVE — ratio thresholds 0.97 / 0.94 are bootstrap estimates.
    Replace with percentile distribution from first 6 months of data.
    """
    try:
        hyg = yf.Ticker("HYG").history(period="3mo")["Close"]
        lqd = yf.Ticker("LQD").history(period="3mo")["Close"]
        # Align on common dates
        combined = hyg.rename("HYG").to_frame().join(lqd.rename("LQD"), how="inner")
        if len(combined) < 20:
            return {"ratio": None, "ratio_norm": None, "regime": "UNKNOWN"}

        ratio      = combined["HYG"] / combined["LQD"]
        ratio_now  = float(ratio.iloc[-1])
        ratio_mean = float(ratio.mean())
        ratio_norm = round(ratio_now / ratio_mean, 4)  # 1.0 = average, <1 = spread widening

        if ratio_norm < 0.94:
            regime = "STRESS"
        elif ratio_norm < 0.97:
            regime = "ELEVATED"
        elif ratio_norm <= 1.03:
            regime = "NORMAL"
        else:
            regime = "TIGHT"

        return {
            "ratio":      round(ratio_now, 4),
            "ratio_norm": ratio_norm,
            "regime":     regime,
        }
    except Exception as e:
        print(f"  [CREDIT] WARNING: {e}")
        return {"ratio": None, "ratio_norm": None, "regime": "UNKNOWN"}


def get_sector_breadth() -> dict:
    """
    % of sector ETFs trading above their 50/150/200-day MAs.
    Returns three breadth metrics; primary signal is pct_above_50ma.
    """
    try:
        results = {"50": [], "150": [], "200": []}
        for etf in SECTOR_ETFS:
            hist = yf.Ticker(etf).history(period="1y")
            if len(hist) < 200:
                continue
            close = hist["Close"]
            price = float(close.iloc[-1])
            for window, key in [(50, "50"), (150, "150"), (200, "200")]:
                ma = float(close.rolling(window).mean().iloc[-1])
                results[key].append(1 if price > ma else 0)

        def _pct(lst):
            return round(sum(lst) / len(lst) * 100, 1) if lst else None

        pct_50  = _pct(results["50"])
        pct_150 = _pct(results["150"])
        pct_200 = _pct(results["200"])

        if pct_50 is None:
            regime = "UNKNOWN"
        elif pct_50 >= 70:
            regime = "BROAD_STRENGTH"
        elif pct_50 >= 50:
            regime = "MIXED"
        elif pct_50 >= 30:
            regime = "WEAKENING"
        else:
            regime = "BROAD_WEAKNESS"

        return {
            "pct_above_50ma":  pct_50,
            "pct_above_150ma": pct_150,
            "pct_above_200ma": pct_200,
            "sectors_checked": len(results["50"]),
            "regime":          regime,
        }
    except Exception as e:
        print(f"  [BREADTH] WARNING: {e}")
        return {"pct_above_50ma": None, "pct_above_150ma": None, "pct_above_200ma": None,
                "sectors_checked": 0, "regime": "UNKNOWN"}


# ── Regime classifier ─────────────────────────────────────────────────────────

def classify_macro(vix: dict, spy: dict, breadth: dict, credit: dict) -> tuple:
    """
    Classify macro regime into: RISK_ON / NEUTRAL / RISK_OFF / CRISIS
    Returns (regime_label: str, macro_score: float)

    Voting model:
      Each signal contributes to an integer score.
      Score >= 3  → RISK_ON
      Score >= 0  → NEUTRAL
      Score >= -2 → RISK_OFF
      Score <  -2 → CRISIS

    Hard overrides:
      VIX CRISIS                      → CRISIS regardless of score
      Credit STRESS + VIX ELEVATED    → RISK_OFF minimum

    macro_score: raw score normalized to [-1, 1] (max ±7 across 4 signals).
    Written to macro_context.json for continuous downstream weighting.

    TODO:DERIVE — vote boundaries and weights from empirical regime analysis
    once 90+ trading days of data are available.
    """
    score = 0

    # VIX (max contribution: +1 / -3 with crisis double-penalty)
    vix_reg = vix.get("regime", "UNKNOWN")
    if vix_reg == "CALM":
        score += 1
    elif vix_reg == "ELEVATED":
        score -= 1
    elif vix_reg == "CRISIS":
        score -= 2   # hard penalty; override fires below too

    # SPY trend (max contribution: +2 / -2)
    spy_reg = spy.get("regime", "UNKNOWN")
    if spy_reg == "BULLISH":
        score += 2
    elif spy_reg == "NEUTRAL":
        score += 1
    elif spy_reg == "BEARISH":
        score -= 2
    # MIXED = 0

    # Sector breadth (max contribution: +1 / -2)
    br_reg = breadth.get("regime", "UNKNOWN")
    if br_reg == "BROAD_STRENGTH":
        score += 1
    elif br_reg == "WEAKENING":
        score -= 1
    elif br_reg == "BROAD_WEAKNESS":
        score -= 2

    # Credit spread (max contribution: +1 / -2)
    cr_reg = credit.get("regime", "UNKNOWN")
    if cr_reg == "TIGHT":
        score += 1
    elif cr_reg == "ELEVATED":
        score -= 1
    elif cr_reg == "STRESS":
        score -= 2

    # Hard overrides
    if vix_reg == "CRISIS":
        return "CRISIS", float(np.clip(score / 7.0, -1.0, 1.0))
    if cr_reg == "STRESS" and vix_reg in ("ELEVATED", "CRISIS"):
        return "RISK_OFF", float(np.clip(score / 7.0, -1.0, 1.0))

    if score >= 3:
        label = "RISK_ON"
    elif score >= 0:
        label = "NEUTRAL"
    elif score >= -2:
        label = "RISK_OFF"
    else:
        label = "CRISIS"

    return label, float(np.clip(score / 7.0, -1.0, 1.0))


# ── Agent summary ─────────────────────────────────────────────────────────────

def build_agent_summary(regime: str, vix: dict, spy: dict,
                         breadth: dict, credit: dict) -> str:
    """Plain-English macro summary. Injected into engine prompts if using agent layer."""
    lines = [
        f"MACRO REGIME: {regime}",
        f"VIX: {vix.get('value', 'N/A')} ({vix.get('regime', 'N/A')})",
        f"SPY: {spy.get('trend_20d', 'N/A')}% 20d trend, "
        f"{'above' if spy.get('above_200ma') else 'below'} 200MA ({spy.get('regime', 'N/A')})",
        f"Sector breadth: {breadth.get('pct_above_50ma', 'N/A')}% above 50MA ({breadth.get('regime', 'N/A')})",
        f"Credit (HYG/LQD norm): {credit.get('ratio_norm', 'N/A')} ({credit.get('regime', 'N/A')})",
    ]
    return " | ".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def build_macro_context() -> dict:
    print("[MacroContext] Fetching macro signals...")

    print("  Fetching VIX...")
    vix = get_vix()

    print("  Fetching SPY trend...")
    spy = get_spy_trend()

    print("  Fetching credit spread (HYG/LQD)...")
    credit = get_credit_spread()

    print("  Fetching sector breadth...")
    breadth = get_sector_breadth()

    regime, macro_score = classify_macro(vix, spy, breadth, credit)

    context = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "macro_regime": regime,
        "macro_score":  macro_score,
        "signals": {
            "vix":            vix,
            "spy_trend":      spy,
            "credit_spread":  credit,
            "sector_breadth": breadth,
        },
        "agent_summary": build_agent_summary(regime, vix, spy, breadth, credit),
    }

    tmp = MACRO_CONTEXT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(context, f, indent=2)
    os.replace(tmp, MACRO_CONTEXT_PATH)

    print(f"\n[MacroContext] Regime: {regime}  Score: {macro_score:+.3f}")
    print(f"[MacroContext] Written → {MACRO_CONTEXT_PATH}")
    return context


def print_summary():
    if not os.path.exists(MACRO_CONTEXT_PATH):
        print("[MacroContext] macro_context.json not found. Run without --summary first.")
        return
    with open(MACRO_CONTEXT_PATH, "r") as f:
        ctx = json.load(f)
    print(f"\n{'='*62}")
    print(f"  MACRO CONTEXT — {ctx.get('timestamp', 'N/A')[:19]}")
    print(f"{'='*62}")
    print(f"  Regime:      {ctx.get('macro_regime', 'N/A')}")
    print(f"  Score:       {ctx.get('macro_score', 'N/A'):+.3f}")
    print(f"\n  {ctx.get('agent_summary', '')}")
    print(f"\n  Raw signals:")
    for key, val in ctx.get("signals", {}).items():
        print(f"    {key:20s}: {val}")
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Macro Context Engine")
    parser.add_argument("--summary", action="store_true", help="Print current macro_context.json")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        build_macro_context()
