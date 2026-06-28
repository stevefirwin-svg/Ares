"""
ares_hold_monitor.py — Ares Hold Monitor (Engine-Dispatched)
=============================================================
Engine-aware hold health scoring. Does NOT use signals.py or Raptor factors.
Each engine has its own health model — no shared layer weights.

Dispatch:
    Engine B → hold_monitor_b.score_engine_b_positions()
    Engine A → hold_monitor_a.score_engine_a_positions()
    Engine F → hold_monitor_f.score_engine_f_positions()
    Engine C → hold_monitor_c.score_engine_c_positions()
    Engine E → hold_monitor_e.score_engine_e_positions()

Output:
    ares_hold_health.json — per-symbol health records (read by ares_exit_monitor)

Run:
    python ares_hold_monitor.py            # full run
    python ares_hold_monitor.py --debug    # verbose per-layer output

Scheduled via Task Scheduler:
    9:52 AM ET  — morning health check (before exit monitor at 9:55)
    4:00 PM ET  — afternoon re-score
"""

import json
import logging
import os
import sys
import argparse
from datetime import datetime
from typing import Dict, List

import pandas as pd

from ares_config import ENGINE_STATUS, BARS_LOOKBACK
from ledger import Ledger
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AresHold] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/ares_hold_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.hold_monitor")

HEALTH_FILE = "ares_hold_health.json"


# ── Bar fetch ─────────────────────────────────────────────────────────────────

from bars_fetch import fetch_bars as _bars_fetch_shared

def fetch_bars(symbols: List[str], lookback: int = BARS_LOOKBACK) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance.

    Thin wrapper around the shared implementation in bars_fetch.py.
    (This file's fetch_bars was on the old Alpaca/IEX endpoint until
    2026-06-28 — see bars_fetch.py's docstring for the full story.)
    """
    return _bars_fetch_shared(symbols, lookback=lookback, min_bars=5)


# ── Persistence ───────────────────────────────────────────────────────────────

def load_health() -> dict:
    if os.path.exists(HEALTH_FILE):
        try:
            with open(HEALTH_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_health(health: dict):
    tmp = HEALTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(health, f, indent=2, default=str)
    os.replace(tmp, HEALTH_FILE)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_monitor(debug: bool = False) -> dict:
    logger.info("=" * 60)
    logger.info("ARES HOLD MONITOR — %s", datetime.now().isoformat())
    logger.info("=" * 60)

    ledger      = Ledger()
    all_symbols = list(ledger.get_all_held_symbols())

    if not all_symbols:
        logger.info("No open Ares positions.")
        return {}

    logger.info("Scoring %d held symbols...", len(all_symbols))
    bars_map = fetch_bars(all_symbols)

    health = load_health()
    all_records = []

    # ── Engine B ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("B") in ("shadow", "live"):
        from hold_monitor_b import score_engine_b_positions
        positions_b = ledger.get_positions("B")
        if positions_b:
            records_b = score_engine_b_positions(positions_b, bars_map)
            all_records.extend(records_b)
            for r in records_b:
                if debug:
                    logger.info(
                        "  B | %s | tier=%-14s | score=%+.3f | pnl=%+.2f%% | "
                        "layers=%s",
                        r["symbol"], r["tier"], r.get("score", 0),
                        r.get("pnl_pct", 0),
                        {k: f"{v:.2f}" for k, v in r.get("layers", {}).items()}
                    )

    # ── Engine A ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("A") in ("shadow", "live"):
        from hold_monitor_a import score_engine_a_positions
        positions_a = ledger.get_positions("A")
        if positions_a:
            records_a = score_engine_a_positions(positions_a, bars_map)
            all_records.extend(records_a)
            for r in records_a:
                if debug:
                    logger.info(
                        "  A | %s | tier=%-14s | score=%+.3f | pnl=%+.2f%% | "
                        "adx=%.1f(Δ%.1f) | layers=%s",
                        r["symbol"], r["tier"], r.get("score", 0),
                        r.get("pnl_pct", 0),
                        r.get("adx", 0), r.get("adx_change", 0),
                        {k: f"{v:.2f}" for k, v in r.get("layers", {}).items()}
                    )

    # ── Engine F ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("F") in ("shadow", "live"):
        from hold_monitor_f import score_engine_f_positions
        positions_f = ledger.get_positions("F")
        if positions_f:
            records_f = score_engine_f_positions(positions_f, bars_map)
            all_records.extend(records_f)
            for r in records_f:
                if debug:
                    logger.info(
                        "  F | %s | tier=%-14s | score=%+.3f | pnl=%+.2f%% | "
                        "progress=%.1f%% | layers=%s",
                        r["symbol"], r["tier"], r.get("score", 0),
                        r.get("pnl_pct", 0), r.get("progress_pct", 0),
                        {k: f"{v:.2f}" for k, v in r.get("layers", {}).items()}
                    )

    # ── Engine C ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("C") in ("shadow", "live"):
        from hold_monitor_c import score_engine_c_positions
        positions_c = ledger.get_positions("C")
        if positions_c:
            records_c = score_engine_c_positions(positions_c, bars_map)
            all_records.extend(records_c)
            for r in records_c:
                if debug:
                    logger.info(
                        "  C | %s | tier=%-14s | score=%+.3f | pnl=%+.2f%% | "
                        "phase=%s | layers=%s",
                        r["symbol"], r["tier"], r.get("score", 0),
                        r.get("pnl_pct", 0), r.get("phase", "?"),
                        {k: f"{v:.2f}" for k, v in r.get("layers", {}).items()}
                    )

    # ── Engine E ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("E") in ("shadow", "live"):
        from hold_monitor_e import score_engine_e_positions
        positions_e = ledger.get_positions("E")
        if positions_e:
            records_e = score_engine_e_positions(positions_e, bars_map)
            all_records.extend(records_e)
            for r in records_e:
                if debug:
                    logger.info(
                        "  E | %s | tier=%-14s | score=%+.3f | pnl=%+.2f%% | "
                        "rs10d=%+.3f | layers=%s",
                        r["symbol"], r["tier"], r.get("score", 0),
                        r.get("pnl_pct", 0), r.get("rs_10d", 0),
                        {k: f"{v:.2f}" for k, v in r.get("layers", {}).items()}
                    )

    # ── Write health file ─────────────────────────────────────────────────────
    for r in all_records:
        sym = r["symbol"]
        health[sym] = r

    health["_last_run"] = datetime.now().isoformat()
    save_health(health)
    logger.info("Health scores written for %d position(s) → %s",
                len(all_records), HEALTH_FILE)

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("  %-8s %-6s %-14s %7s %8s %8s",
                "Symbol", "Engine", "Tier", "Score", "P&L%", "Days")
    logger.info("  " + "-"*60)
    for r in sorted(all_records, key=lambda x: x.get("score", 0) or 0, reverse=True):
        tier_color = {
            "STRENGTHENING": "▲",
            "STABLE":        "─",
            "DECAYING":      "▼",
            "EXHAUSTED":     "✗",
        }.get(r["tier"], "?")
        logger.info("  %-8s %-6s %s %-13s %+6.3f  %+6.2f%%  %3dd",
                    r["symbol"], r.get("engine_id", "?"),
                    tier_color, r["tier"],
                    r.get("score") or 0,
                    r.get("pnl_pct") or 0,
                    r.get("days_held") or 0)

    return {r["symbol"]: r for r in all_records}


def build_health_html() -> str:
    """
    Returns an HTML snippet for the daily_recap email.
    Called by daily_recap.py.
    """
    health = load_health()
    records = [v for k, v in health.items() if k != "_last_run" and isinstance(v, dict)]

    if not records:
        return '<div style="color:#6a6a8a">No Ares positions to score.</div>'

    tier_color = {
        "STRENGTHENING": "#00d4aa",
        "STABLE":        "#c0c0d0",
        "DECAYING":      "#f0a500",
        "EXHAUSTED":     "#ff6b6b",
        "INSUFFICIENT_DATA": "#4a4a6a",
    }

    rows = []
    for r in sorted(records, key=lambda x: x.get("score") or 0, reverse=True):
        tier  = r.get("tier", "?")
        score = r.get("score")
        col   = tier_color.get(tier, "#6a6a8a")
        rows.append(f"""
        <tr style="border-bottom:1px solid #1e1e34">
          <td style="padding:6px 10px;color:#e0e0f0">{r.get("symbol","?")}</td>
          <td style="padding:6px 10px;color:#6a6a8a">{r.get("engine_id","?")}</td>
          <td style="padding:6px 10px;color:{col}">{tier}</td>
          <td style="padding:6px 10px;text-align:right;color:{col}">{f"{score:+.3f}" if score is not None else "—"}</td>
          <td style="padding:6px 10px;text-align:right;color:#c0c0d0">{r.get("pnl_pct", 0):+.2f}%</td>
          <td style="padding:6px 10px;text-align:right;color:#6a6a8a">{r.get("days_held", 0)}d</td>
        </tr>""")

    return f"""
    <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Position Health</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr style="border-bottom:2px solid #2a2a3e">
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Symbol</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Engine</th>
        <th style="padding:6px 10px;text-align:left;color:#00d4aa;font-size:11px">Tier</th>
        <th style="padding:6px 10px;text-align:right;color:#00d4aa;font-size:11px">Score</th>
        <th style="padding:6px 10px;text-align:right;color:#00d4aa;font-size:11px">P&L%</th>
        <th style="padding:6px 10px;text-align:right;color:#00d4aa;font-size:11px">Days</th>
      </tr>
      {"".join(rows)}
    </table>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Hold Monitor")
    parser.add_argument("--debug", action="store_true", help="Verbose per-layer output")
    args = parser.parse_args()
    run_monitor(debug=args.debug)
