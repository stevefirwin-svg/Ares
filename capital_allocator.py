"""
capital_allocator.py — Ares Advisory Capital Allocator
=======================================================
Computes recommended engine weight adjustments based on:
  - Rolling Sharpe (20-trade window per engine)
  - Win rate per engine
  - IC vs forward returns (once forward data available)

OUTPUT IS ADVISORY ONLY. No automatic reallocation.
Steve approves every weight shift manually.

Gates:
  - Requires >= 15 closed trades per engine before firing (REALLOCATION_GATE_TRADES)
  - No engine below ENGINE_CAPITAL_FLOOR (10%) while active
  - Engine D always stays at 0% until intraday runtime is built

Run:
    python capital_allocator.py --status     # show current state + recommendation
    python capital_allocator.py --report     # full advisory report (end-of-day)
    python capital_allocator.py --approve    # interactive approval flow (not yet implemented)
"""

import json
import os
import argparse
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, Tuple

from ares_config import (
    INITIAL_ENGINE_WEIGHTS,
    ENGINE_CAPITAL_FLOOR,
    ENGINE_STATUS,
    REALLOCATION_GATE_TRADES,
    REALLOCATION_LOOKBACK,
    REALLOCATION_MIN_WEIGHT,
    TOTAL_EQUITY,
    IC_CALIBRATION_GATE,
    CASH_FLOOR_PCT,        # RF-8: 10% cash buffer never deployed
)

OUTCOMES_FILE   = "sub_engine_outcomes.json"
ALLOCATOR_STATE = "capital_allocator_state.json"

# ── Data loading ──────────────────────────────────────────────────────────────

def _load_outcomes() -> list:
    if not os.path.exists(OUTCOMES_FILE):
        return []
    with open(OUTCOMES_FILE) as f:
        data = json.load(f)
    return data.get("trades", [])


def _load_state() -> dict:
    if os.path.exists(ALLOCATOR_STATE):
        with open(ALLOCATOR_STATE) as f:
            return json.load(f)
    return {
        "current_weights":      dict(INITIAL_ENGINE_WEIGHTS),
        "last_reallocation":    None,
        "reallocation_history": [],
    }


def _save_state(state: dict):
    tmp = ALLOCATOR_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, ALLOCATOR_STATE)


# ── Per-engine metrics ────────────────────────────────────────────────────────

def compute_engine_metrics(trades: list, engine_id: str) -> dict:
    """
    Compute rolling metrics for a single engine.
    Uses last REALLOCATION_LOOKBACK closed trades (terminal exits only).
    """
    engine_trades = [
        t for t in trades
        if t.get("engine_id") == engine_id and t.get("pnl_pct") is not None
    ]

    total_closed = len(engine_trades)

    if total_closed == 0:
        return {
            "engine_id":        engine_id,
            "total_closed":     0,
            "gate_met":         False,
            "rolling_n":        0,
            "win_rate":         None,
            "rolling_sharpe":   None,
            "avg_pnl_pct":      None,
            "ic_valid_count":   0,
            "ic_gate_met":      False,
        }

    # Rolling window — most recent N trades
    window = engine_trades[-REALLOCATION_LOOKBACK:]
    pnls   = [t["pnl_pct"] / 100.0 for t in window]  # fractional returns

    wins     = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) * 100

    avg_pnl  = sum(pnls) / len(pnls)

    # Sharpe approximation on trade-level returns (not time-series)
    # Annualization not applied — relative comparison between engines is sufficient
    # TODO:DERIVE — once we have hold_days, compute time-weighted Sharpe
    import math
    if len(pnls) >= 2:
        mean = avg_pnl
        variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std  = math.sqrt(variance) if variance > 0 else 1e-9
        sharpe = mean / std
    else:
        sharpe = 0.0

    ic_valid_count = sum(1 for t in engine_trades if t.get("ic_valid"))

    return {
        "engine_id":        engine_id,
        "total_closed":     total_closed,
        "gate_met":         total_closed >= REALLOCATION_GATE_TRADES,
        "rolling_n":        len(window),
        "win_rate":         round(win_rate, 1),
        "rolling_sharpe":   round(sharpe, 4),
        "avg_pnl_pct":      round(avg_pnl * 100, 3),
        "ic_valid_count":   ic_valid_count,
        "ic_gate_met":      ic_valid_count >= IC_CALIBRATION_GATE,
    }


# ── Advisory weight calculation ───────────────────────────────────────────────

def compute_advisory_weights(metrics: Dict[str, dict], current_weights: dict) -> Tuple[dict, str]:
    """
    Compute advisory target weights from engine metrics.

    Method:
      1. Score each eligible engine: 0.5 × normalized_sharpe + 0.5 × normalized_win_rate
      2. Engines below gate get current weight (no change until gate met)
      3. Normalize eligible engine scores to sum to (1.0 - ineligible_weight_sum)
      4. Apply floor (ENGINE_CAPITAL_FLOOR) — no engine below 10%
      5. Engine D always 0% regardless

    Returns (advisory_weights, rationale_string)
    """
    active_engines = [e for e, s in ENGINE_STATUS.items() if s != "off"]
    eligible       = [e for e in active_engines if metrics.get(e, {}).get("gate_met")]
    ineligible     = [e for e in active_engines if not metrics.get(e, {}).get("gate_met")]

    if not eligible:
        return current_weights, "No engines have met the gate — maintaining current weights."

    # Compute scores for eligible engines
    sharpes  = {e: metrics[e]["rolling_sharpe"] for e in eligible}
    win_rates = {e: metrics[e]["win_rate"] for e in eligible}

    def _normalize(vals: dict) -> dict:
        mn, mx = min(vals.values()), max(vals.values())
        if mx == mn:
            return {k: 0.5 for k in vals}
        return {k: (v - mn) / (mx - mn) for k, v in vals.items()}

    norm_sharpe   = _normalize(sharpes)
    norm_win_rate = _normalize(win_rates)

    scores = {
        e: 0.5 * norm_sharpe[e] + 0.5 * norm_win_rate[e]
        for e in eligible
    }

    # RF-8: Engine weights sum to (1 - CASH_FLOOR_PCT), not 1.0
    # The remaining CASH_FLOOR_PCT is permanently held as cash buffer
    DEPLOYABLE_FRACTION = 1.0 - CASH_FLOOR_PCT  # e.g. 0.90 if floor=10%

    # Weight locked to current for ineligible engines
    ineligible_weight = sum(current_weights.get(e, 0.0) for e in ineligible)
    total_for_eligible = max(
        0.0,
        DEPLOYABLE_FRACTION - ineligible_weight - current_weights.get("D", 0.0)
    )

    # Distribute total_for_eligible proportionally to scores
    total_score = sum(scores.values()) or 1.0
    raw_weights = {e: (scores[e] / total_score) * total_for_eligible for e in eligible}

    # Apply floor
    advisory = {}
    for e in active_engines:
        if e == "D":
            advisory[e] = 0.0
        elif e in ineligible:
            advisory[e] = current_weights.get(e, INITIAL_ENGINE_WEIGHTS.get(e, 0.0))
        else:
            w = raw_weights.get(e, 0.0)
            advisory[e] = max(w, REALLOCATION_MIN_WEIGHT)

    # Re-normalize so active engines sum to DEPLOYABLE_FRACTION (RF-8: not 1.0)
    active_sum = sum(advisory[e] for e in active_engines if e != "D")
    if active_sum > 0:
        target = DEPLOYABLE_FRACTION
        for e in active_engines:
            if e != "D":
                advisory[e] = round(advisory[e] / active_sum * target, 4)

    advisory["D"] = 0.0

    rationale_lines = [
        f"  Advisory based on {len(eligible)} eligible engines "
        f"({REALLOCATION_LOOKBACK}-trade rolling window):",
    ]
    for e in sorted(eligible):
        m = metrics[e]
        rationale_lines.append(
            f"    Engine {e}: sharpe={m['rolling_sharpe']:+.3f}  "
            f"win%={m['win_rate']:.1f}%  score={scores[e]:.3f}  "
            f"→ {advisory[e]:.1%}"
        )
    for e in sorted(ineligible):
        m = metrics[e]
        rationale_lines.append(
            f"    Engine {e}: {m['total_closed']}/{REALLOCATION_GATE_TRADES} trades — gate not met, weight held"
        )

    return advisory, "\n".join(rationale_lines)


# ── Status / report ───────────────────────────────────────────────────────────

def print_status():
    trades  = _load_outcomes()
    state   = _load_state()
    current = state["current_weights"]

    all_metrics = {
        e: compute_engine_metrics(trades, e)
        for e in INITIAL_ENGINE_WEIGHTS
    }

    advisory, rationale = compute_advisory_weights(all_metrics, current)

    print(f"\n{'='*72}")
    print(f"  ARES CAPITAL ALLOCATOR STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*72}")
    print(f"  Total equity (config):  ${TOTAL_EQUITY:>10,.0f}")
    print(f"  Cash floor (held):      ${TOTAL_EQUITY * CASH_FLOOR_PCT:>10,.0f}  ({CASH_FLOOR_PCT:.0%} — RF-8)")
    print(f"  Deployable equity:      ${TOTAL_EQUITY * (1-CASH_FLOOR_PCT):>10,.0f}")
    print(f"  Last reallocation:      {state.get('last_reallocation') or 'Never'}")
    print()
    print(f"  {'Engine':<8} {'Status':<8} {'Cur Wt':>8} {'Cur $':>10} "
          f"{'Trades':>7} {'Gate':>6} {'Win%':>6} {'Sharpe':>8} {'Adv Wt':>8} {'Adv $':>10}")
    print(f"  {'-'*8} {'-'*8} {'-'*8} {'-'*10} {'-'*7} {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*10}")

    has_advisory_change = False
    for eid in sorted(INITIAL_ENGINE_WEIGHTS.keys()):
        m       = all_metrics[eid]
        cur_w   = current.get(eid, 0.0)
        adv_w   = advisory.get(eid, 0.0)
        cur_usd = TOTAL_EQUITY * cur_w
        adv_usd = TOTAL_EQUITY * adv_w
        status  = ENGINE_STATUS.get(eid, "off")
        gate    = "✓" if m["gate_met"] else f"{m['total_closed']}/{REALLOCATION_GATE_TRADES}"
        win_pct = f"{m['win_rate']:.1f}%" if m["win_rate"] is not None else "—"
        sharpe  = f"{m['rolling_sharpe']:+.3f}" if m["rolling_sharpe"] is not None else "—"
        change  = "◀" if abs(adv_w - cur_w) > 0.005 else " "
        if change == "◀":
            has_advisory_change = True
        print(f"  {eid:<8} {status:<8} {cur_w:>7.1%} ${cur_usd:>9,.0f} "
              f"{m['total_closed']:>7} {gate:>6} {win_pct:>6} {sharpe:>8} "
              f"{adv_w:>7.1%} ${adv_usd:>9,.0f} {change}")

    print()
    if has_advisory_change:
        print(f"  ⚡ ADVISORY REALLOCATION AVAILABLE:")
        print(rationale)
        print()
        print(f"  To approve, manually update INITIAL_ENGINE_WEIGHTS in ares_config.py")
        print(f"  then run: python capital_allocator.py --status to confirm.")
    else:
        print(f"  Current weights match advisory — no reallocation needed.")

    if state.get("reallocation_history"):
        print(f"\n  Reallocation history ({len(state['reallocation_history'])} events):")
        for h in state["reallocation_history"][-3:]:
            print(f"    {h.get('date','?')}: {h.get('note','')}")

    print(f"{'='*72}\n")


def print_report():
    """End-of-day advisory report — more verbose than --status."""
    trades    = _load_outcomes()
    state     = _load_state()
    current   = state["current_weights"]

    all_metrics = {
        e: compute_engine_metrics(trades, e)
        for e in INITIAL_ENGINE_WEIGHTS
    }

    advisory, rationale = compute_advisory_weights(all_metrics, current)

    print(f"\n{'='*72}")
    print(f"  ARES CAPITAL ALLOCATOR — END OF DAY REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"{'='*72}")

    for eid in sorted(INITIAL_ENGINE_WEIGHTS.keys()):
        m      = all_metrics[eid]
        status = ENGINE_STATUS.get(eid, "off")
        print(f"\n  Engine {eid} [{status.upper()}]")
        print(f"    Closed trades:   {m['total_closed']}  (gate: {REALLOCATION_GATE_TRADES})")
        print(f"    IC-valid:        {m['ic_valid_count']}  (IC gate: {IC_CALIBRATION_GATE})")
        if m["gate_met"]:
            print(f"    Win rate:        {m['win_rate']:.1f}%  (rolling {m['rolling_n']} trades)")
            print(f"    Rolling Sharpe:  {m['rolling_sharpe']:+.4f}")
            print(f"    Avg P&L:         {m['avg_pnl_pct']:+.3f}%")
        else:
            print(f"    Gate not met — {REALLOCATION_GATE_TRADES - m['total_closed']} more trades needed")

    print(f"\n  Advisory weights:")
    print(rationale)
    print(f"\n  Advisory is informational only. No automatic reallocation.")
    print(f"{'='*72}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Advisory Capital Allocator")
    parser.add_argument("--status", action="store_true", help="Current state + advisory")
    parser.add_argument("--report", action="store_true", help="Full end-of-day report")
    args = parser.parse_args()

    if args.report:
        print_report()
    else:
        print_status()
