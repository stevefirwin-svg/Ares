"""
daily_recap.py — Ares Daily Recap Email
========================================
Sends a daily HTML email with:
  - Account summary (equity, cash, P&L)
  - Per-engine table: open positions, today's activity, cumulative P&L
  - Macro regime status
  - Capital allocation vs advisory
  - Portfolio analytics per engine (win rate, avg P&L, Sharpe)
  - Current holdings with P&L + days held

Usage:
  python daily_recap.py              # Send recap email
  python daily_recap.py --preview    # Save HTML locally, don't email

Schedule at 4:15 PM ET via Task Scheduler.
"""

import logging
import os
import json
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

import numpy as np

from ares_config import (
    INITIAL_ENGINE_WEIGHTS,
    ENGINE_STATUS,
    TOTAL_EQUITY,
    REALLOCATION_GATE_TRADES,
)
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("ares.recap")

EMAIL_SENDER   = "stevefirwin@gmail.com"
EMAIL_PASSWORD = "trhy qqzo kylt jker"
EMAIL_RECEIVER = "stevefirwin@gmail.com"

LEDGER_FILE   = "ares_position_ledger.json"
OUTCOMES_FILE = "sub_engine_outcomes.json"
MACRO_FILE    = "macro_context.json"
ALLOC_FILE    = "capital_allocator_state.json"

# ── Data loaders ──────────────────────────────────────────────────────────────

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def get_account_data():
    from ares_config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
    import requests
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        acct = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10)
        acct.raise_for_status()
        account = acct.json()
    except Exception as e:
        logger.warning(f"Account fetch failed: {e}")
        account = {}

    try:
        pos = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=headers, timeout=10)
        pos.raise_for_status()
        positions = pos.json()
    except Exception as e:
        logger.warning(f"Positions fetch failed: {e}")
        positions = []

    return account, positions


def get_engine_stats(outcomes: list, engine_id: str) -> dict:
    trades = [t for t in outcomes if t.get("engine_id") == engine_id and t.get("pnl_pct") is not None]
    if not trades:
        return {"count": 0, "win_rate": None, "avg_pnl": None, "net_pnl": 0.0, "sharpe": None}

    pnls   = [t["pnl_pct"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    net    = sum(t.get("pnl", 0) or 0 for t in trades)
    avg    = sum(pnls) / len(pnls)

    import math
    if len(pnls) >= 2:
        mean = avg / 100
        vals = [p / 100 for p in pnls]
        var  = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        std  = math.sqrt(var) if var > 0 else 1e-9
        sharpe = round(mean / std, 3)
    else:
        sharpe = None

    return {
        "count":    len(trades),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "avg_pnl":  round(avg, 2),
        "net_pnl":  round(net, 2),
        "sharpe":   sharpe,
    }


def get_todays_closed(ledger: dict) -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    return [t for t in ledger.get("closed", []) if str(t.get("exit_date", ""))[:10] == today]


def get_todays_entries(ledger: dict) -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    return [
        v for v in ledger.get("positions", {}).values()
        if str(v.get("entry_date", ""))[:10] == today
    ]


# ── HTML builders ─────────────────────────────────────────────────────────────

def _color(val, positive_good=True):
    """Return CSS color for a numeric value."""
    if val is None:
        return "#6a6a8a"
    if val > 0:
        return "#00d4aa" if positive_good else "#ff6b6b"
    if val < 0:
        return "#ff6b6b" if positive_good else "#00d4aa"
    return "#c0c0d0"


def build_engine_table(ledger: dict, outcomes: list, positions_live: list) -> str:
    """Per-engine summary table — core of the recap."""
    # Map live positions by symbol for current price lookup
    live_by_symbol = {p["symbol"]: p for p in positions_live}

    rows = []
    for eid in sorted(ENGINE_STATUS.keys()):
        status   = ENGINE_STATUS.get(eid, "off")
        stats    = get_engine_stats(outcomes, eid)
        weight   = INITIAL_ENGINE_WEIGHTS.get(eid, 0.0)
        alloc_usd = TOTAL_EQUITY * weight

        open_pos = [
            v for v in ledger.get("positions", {}).values()
            if v.get("engine_id") == eid
        ]
        deployed = sum(p.get("entry_price", 0) * p.get("shares", 0) for p in open_pos)

        gate_str = f"{stats['count']}/{REALLOCATION_GATE_TRADES}"
        win_str  = f"{stats['win_rate']:.1f}%" if stats["win_rate"] is not None else "—"
        avg_str  = f"{stats['avg_pnl']:+.2f}%" if stats["avg_pnl"] is not None else "—"
        net_str  = f"${stats['net_pnl']:+,.0f}" if stats["count"] > 0 else "—"
        sharpe_s = f"{stats['sharpe']:+.3f}" if stats["sharpe"] is not None else "—"
        avg_col  = _color(stats["avg_pnl"])
        net_col  = _color(stats["net_pnl"])

        status_color = {"shadow": "#f0a500", "live": "#00d4aa", "off": "#4a4a6a"}.get(status, "#6a6a8a")

        rows.append(f"""
        <tr style="border-bottom:1px solid #1e1e34">
          <td style="padding:8px 10px;font-weight:700;color:#e0e0f0">{eid}</td>
          <td style="padding:8px 10px;color:{status_color};font-size:11px;text-transform:uppercase">{status}</td>
          <td style="padding:8px 10px;text-align:right;color:#c0c0d0">${alloc_usd:,.0f}</td>
          <td style="padding:8px 10px;text-align:right;color:#c0c0d0">{len(open_pos)}</td>
          <td style="padding:8px 10px;text-align:right;color:#c0c0d0">${deployed:,.0f}</td>
          <td style="padding:8px 10px;text-align:right;color:#c0c0d0">{gate_str}</td>
          <td style="padding:8px 10px;text-align:right;color:#c0c0d0">{win_str}</td>
          <td style="padding:8px 10px;text-align:right;color:{avg_col}">{avg_str}</td>
          <td style="padding:8px 10px;text-align:right;color:{net_col}">{net_str}</td>
          <td style="padding:8px 10px;text-align:right;color:#c0c0d0">{sharpe_s}</td>
        </tr>""")

    header = """
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr style="border-bottom:2px solid #2a2a3e">
        <th style="padding:8px 10px;text-align:left;color:#00d4aa;font-size:11px">Engine</th>
        <th style="padding:8px 10px;text-align:left;color:#00d4aa;font-size:11px">Status</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Alloc</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Open</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Deployed</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Trades/Gate</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Win%</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Avg P&L</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Net P&L</th>
        <th style="padding:8px 10px;text-align:right;color:#00d4aa;font-size:11px">Sharpe</th>
      </tr>
      {"".join(rows)}
    </table>"""
    return header


def build_holdings_rows(ledger: dict, positions_live: list) -> str:
    live_by_symbol = {p["symbol"]: p for p in positions_live}
    open_pos = ledger.get("positions", {}).values()
    if not open_pos:
        return '<tr><td colspan="8" style="padding:12px;color:#6a6a8a;text-align:center">No open positions</td></tr>'

    rows = []
    today = datetime.now().date()
    for p in sorted(open_pos, key=lambda x: x.get("engine_id", "")):
        sym         = p.get("symbol", "?")
        eid         = p.get("engine_id", "?")
        shares      = p.get("shares", 0)
        entry_price = p.get("entry_price", 0)
        entry_date  = p.get("entry_date", "")
        try:
            days_held = (today - datetime.strptime(str(entry_date)[:10], "%Y-%m-%d").date()).days
        except Exception:
            days_held = "?"

        live = live_by_symbol.get(sym, {})
        cur_price  = float(live.get("current_price", entry_price) or entry_price)
        unreal_pct = ((cur_price / entry_price) - 1) * 100 if entry_price else 0
        unreal_usd = (cur_price - entry_price) * shares
        pnl_col    = _color(unreal_pct)

        rows.append(f"""
        <tr style="border-bottom:1px solid #1e1e34">
          <td style="padding:7px 10px;color:#e0e0f0;font-weight:600">{sym}</td>
          <td style="padding:7px 10px;color:#6a6a8a">{eid}</td>
          <td style="padding:7px 10px;text-align:right;color:#c0c0d0">{shares}</td>
          <td style="padding:7px 10px;text-align:right;color:#c0c0d0">${entry_price:.2f}</td>
          <td style="padding:7px 10px;text-align:right;color:#c0c0d0">${cur_price:.2f}</td>
          <td style="padding:7px 10px;text-align:right;color:{pnl_col}">{unreal_pct:+.2f}%</td>
          <td style="padding:7px 10px;text-align:right;color:{pnl_col}">${unreal_usd:+,.0f}</td>
          <td style="padding:7px 10px;text-align:right;color:#6a6a8a">{days_held}d</td>
        </tr>""")
    return "".join(rows)


def build_todays_activity(ledger: dict) -> str:
    entries = get_todays_entries(ledger)
    exits   = get_todays_closed(ledger)

    lines = []
    for e in entries:
        lines.append(
            f'<div style="color:#00d4aa">▲ ENTRY  [{e.get("engine_id","?")}] '
            f'{e.get("symbol","?")}  {e.get("shares","?")} shares @ ${e.get("entry_price",0):.2f}</div>'
        )
    for e in exits:
        pnl     = e.get("pnl_pct")
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "N/A"
        col     = _color(pnl)
        lines.append(
            f'<div style="color:{col}">▼ EXIT   [{e.get("engine_id","?")}] '
            f'{e.get("symbol","?")}  {pnl_str}  ({e.get("exit_reason","?")})</div>'
        )

    if not lines:
        return '<div style="color:#6a6a8a">No entries or exits today.</div>'
    return "\n".join(lines)


def build_macro_section(macro: dict) -> str:
    regime = macro.get("macro_regime", "UNKNOWN")
    score  = macro.get("macro_score", 0)
    sigs   = macro.get("signals", {})
    ts     = str(macro.get("timestamp", ""))[:16]

    regime_color = {
        "RISK_ON": "#00d4aa", "NEUTRAL": "#f0a500",
        "RISK_OFF": "#ff8c00", "CRISIS": "#ff4444"
    }.get(regime, "#6a6a8a")

    vix    = sigs.get("vix", {})
    spy    = sigs.get("spy_trend", {})
    credit = sigs.get("credit_spread", {})
    bread  = sigs.get("sector_breadth", {})

    return f"""
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      <div style="flex:1;min-width:120px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Regime</div>
        <div style="color:{regime_color};font-size:18px;font-weight:700">{regime}</div>
        <div style="color:#6a6a8a;font-size:10px">score {score:+.3f}</div>
      </div>
      <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">VIX</div>
        <div style="color:#c0c0d0;font-size:16px;font-weight:700">{vix.get('value','—')}</div>
        <div style="color:#6a6a8a;font-size:10px">{vix.get('regime','—')}</div>
      </div>
      <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">SPY 20d</div>
        <div style="color:#c0c0d0;font-size:16px;font-weight:700">{spy.get('trend_20d','—')}%</div>
        <div style="color:#6a6a8a;font-size:10px">{spy.get('regime','—')}</div>
      </div>
      <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Credit</div>
        <div style="color:#c0c0d0;font-size:16px;font-weight:700">{credit.get('ratio_norm','—')}</div>
        <div style="color:#6a6a8a;font-size:10px">{credit.get('regime','—')}</div>
      </div>
      <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
        <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Breadth</div>
        <div style="color:#c0c0d0;font-size:16px;font-weight:700">{bread.get('pct_above_50ma','—')}%</div>
        <div style="color:#6a6a8a;font-size:10px">{bread.get('regime','—')}</div>
      </div>
    </div>
    <div style="color:#3a3a5e;font-size:10px;margin-top:6px">Last updated: {ts} UTC</div>"""


# ── Main HTML ─────────────────────────────────────────────────────────────────

def build_html(account: dict, positions_live: list, ledger: dict,
               outcomes: list, macro: dict) -> str:
    equity  = float(account.get("equity", 0))
    cash    = float(account.get("cash", 0))
    bp      = float(account.get("buying_power", 0))
    total_mv = sum(abs(float(p.get("qty", 0))) * float(p.get("current_price", 0)) for p in positions_live)
    util     = total_mv / equity * 100 if equity > 0 else 0
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    today_closed  = get_todays_closed(ledger)
    today_pnl     = sum(t.get("pnl", 0) or 0 for t in today_closed)
    today_pnl_col = _color(today_pnl)

    engine_table   = build_engine_table(ledger, outcomes, positions_live)
    holdings_rows  = build_holdings_rows(ledger, positions_live)
    activity_html  = build_todays_activity(ledger)
    macro_html     = build_macro_section(macro)

    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0;background:#07071a;font-family:'Helvetica Neue',Arial,sans-serif">
    <table width="700" align="center" cellpadding="0" cellspacing="0"
           style="background:#0a0a1e;border:1px solid #1e1e34;border-radius:8px;overflow:hidden">

      <!-- Header -->
      <tr><td style="background:linear-gradient(135deg,#0d0d2b,#1a1a40);padding:20px 28px;border-bottom:1px solid #2a2a3e">
        <div style="font-size:22px;font-weight:800;color:#00d4aa;letter-spacing:2px">ARES</div>
        <div style="color:#6a6a8a;font-size:11px;margin-top:3px">Daily Recap — {date_str}</div>
      </td></tr>

      <!-- Account summary -->
      <tr><td style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Equity</div>
            <div style="color:#e0e0f0;font-size:18px;font-weight:700">${equity:,.0f}</div>
          </div>
          <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Cash</div>
            <div style="color:#c0c0d0;font-size:18px;font-weight:700">${cash:,.0f}</div>
          </div>
          <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Deployed</div>
            <div style="color:#c0c0d0;font-size:18px;font-weight:700">{util:.1f}%</div>
          </div>
          <div style="flex:1;min-width:100px;background:#0e0e20;padding:12px;border-radius:4px;text-align:center">
            <div style="color:#6a6a8a;font-size:10px;text-transform:uppercase">Today P&L</div>
            <div style="color:{today_pnl_col};font-size:18px;font-weight:700">${today_pnl:+,.0f}</div>
            <div style="color:#6a6a8a;font-size:10px">{len(today_closed)} exits</div>
          </div>
        </div>
      </td></tr>

      <!-- Macro regime -->
      <tr><td style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
        <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Macro Regime</div>
        {macro_html}
      </td></tr>

      <!-- Engine table -->
      <tr><td style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
        <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Engine Performance</div>
        {engine_table}
      </td></tr>

      <!-- Today's activity -->
      <tr><td style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
        <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Today's Activity</div>
        <div style="font-size:13px;font-family:'Courier New',monospace;line-height:1.8">{activity_html}</div>
      </td></tr>

      <!-- Holdings -->
      <tr><td style="padding:16px 28px;border-bottom:1px solid #2a2a3e">
        <div style="color:#a0a0b0;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin-bottom:8px">Current Holdings</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="border-bottom:2px solid #2a2a3e">
            <th style="padding:7px 10px;text-align:left;color:#00d4aa;font-size:11px">Symbol</th>
            <th style="padding:7px 10px;text-align:left;color:#00d4aa;font-size:11px">Engine</th>
            <th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px">Shares</th>
            <th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px">Entry</th>
            <th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px">Current</th>
            <th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px">P&L %</th>
            <th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px">P&L $</th>
            <th style="padding:7px 10px;text-align:right;color:#00d4aa;font-size:11px">Days</th>
          </tr>
          {holdings_rows}
        </table>
      </td></tr>

      <!-- Footer -->
      <tr><td style="padding:14px 28px;text-align:center">
        <div style="color:#3a3a5e;font-size:10px">ARES | 6 Independent Engines | Paper Trading | Not Financial Advice</div>
      </td></tr>

    </table>
    </body></html>"""


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(html: str, today_pnl: float):
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg["Subject"] = f"ARES Daily Recap {datetime.now().strftime('%Y-%m-%d')} | P&L ${today_pnl:+,.0f}"
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL_SENDER, EMAIL_PASSWORD)
            s.send_message(msg)
        print("Recap email sent.")
    except Exception as e:
        import traceback
        print(f"Email failed: {e}")
        os.makedirs("logs", exist_ok=True)
        with open("logs/recap_errors.log", "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] send_email FAILED: {e}\n{traceback.format_exc()}\n")


# ── Entry ─────────────────────────────────────────────────────────────────────

def main(preview=False):
    os.makedirs("logs", exist_ok=True)
    try:
        account, positions_live = get_account_data()
        ledger   = load_json(LEDGER_FILE,   {"positions": {}, "closed": []})
        outcomes = load_json(OUTCOMES_FILE, {"trades": []}).get("trades", [])
        macro    = load_json(MACRO_FILE,    {})

        html       = build_html(account, positions_live, ledger, outcomes, macro)
        today_pnl  = sum(t.get("pnl", 0) or 0 for t in get_todays_closed(ledger))

        if preview:
            os.makedirs("logs", exist_ok=True)
            with open("logs/ares_recap_preview.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("Preview saved to logs/ares_recap_preview.html")
        else:
            send_email(html, today_pnl)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[daily_recap] FATAL: {e}\n{tb}")
        with open("logs/recap_errors.log", "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] FATAL: {e}\n{tb}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    main(preview=args.preview)
