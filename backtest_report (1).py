"""
=============================================================================
ELITE BACKTESTING ENGINE WITH FULL REPORT
=============================================================================
Runs a comprehensive backtest and generates a detailed HTML report showing:
  - Equity curve
  - Win/loss statistics
  - Sharpe, Sortino, Calmar ratios
  - Max drawdown analysis
  - Per-symbol breakdown
  - Monthly returns heatmap
  - Trade-by-trade log

USAGE:
  python backtest_report.py          # Backtest last 90 days
  python backtest_report.py --days 180   # Backtest last 180 days
  python backtest_report.py --days 365   # Full year backtest

Output: Opens a beautiful HTML report in your browser automatically.
=============================================================================
"""

import sys
import os
import argparse
import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from utils.indicators import compute_all_indicators
from strategies.signal_engine import generate_composite_signal

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("Backtest")


# =============================================================================
# BACKTESTING ENGINE
# =============================================================================

def fetch_yfinance(symbol: str, days: int = 500) -> pd.DataFrame:
    """
    Fetch historical daily bars using Yahoo Finance (free, no subscription needed).
    Used for backtesting only — live trading still uses Alpaca.
    """
    try:
        import yfinance as yf
        period = "2y" if days <= 500 else "5y"
        df = yf.download(symbol, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty:
            return pd.DataFrame()

        # Fix column names for newer yfinance versions
        df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        # Ensure required columns exist
        required = ['open', 'high', 'low', 'close', 'volume']
        if not all(c in df.columns for c in required):
            return pd.DataFrame()

        df = df[required].dropna()
        df.index = pd.to_datetime(df.index)
        return df

    except Exception as e:
        logger.error(f"yfinance error for {symbol}: {e}")
        return pd.DataFrame()


def run_backtest(data_client, days: int = 365) -> dict:
    """
    Walk-forward backtest on historical data.
    Simulates bar-by-bar signal generation and trade execution.
    """
    logger.info(f"Starting backtest: last {days} days on {len(config.WATCHLIST)} symbols")

    # Override signal threshold for backtesting daily bars
    # Daily bars score lower than intraday — this is expected and normal
    BACKTEST_MIN_SCORE = 0.25  # Much more permissive for daily backtesting

    all_trades   = []
    equity_curve = [config.PORTFOLIO_SIZE]
    portfolio    = config.PORTFOLIO_SIZE

    for symbol in config.WATCHLIST:
        logger.info(f"  Backtesting {symbol}...")
        try:
            # Use Yahoo Finance for free historical data
            df = fetch_yfinance(symbol, days)

            if df.empty:
                logger.warning(f"  {symbol}: no data returned")
                continue

            df = compute_all_indicators(df, config)
            df = df.dropna(subset=['rsi', 'macd', 'adx', 'atr'])

            if len(df) < 60:
                logger.warning(f"  {symbol}: not enough data ({len(df)} bars after indicators)")
                continue

            logger.info(f"  {symbol}: {len(df)} bars available")

            in_trade   = False
            entry_px   = 0.0
            stop       = 0.0
            target     = 0.0
            direction  = 'long'
            entry_date = None
            shares     = 0

            # Use lower warmup (50 bars) so we have more bars to test
            warmup = min(50, len(df) // 4)

            for i in range(warmup, len(df) - 1):
                bar_date = df.index[i]
                slice_df = df.iloc[:i+1].copy()

                if not in_trade:
                    signal_data = generate_composite_signal(slice_df, config)
                    score = signal_data.get('signal_score', 0)

                    if score >= BACKTEST_MIN_SCORE:
                        entry_px  = slice_df['close'].iloc[-1]
                        atr_val   = signal_data['atr']
                        direction = signal_data['direction']

                        # ATR-based stops
                        atr_mult_stop   = 2.0
                        atr_mult_target = 3.0
                        stop   = entry_px - atr_val * atr_mult_stop   if direction == 'long' else entry_px + atr_val * atr_mult_stop
                        target = entry_px + atr_val * atr_mult_target if direction == 'long' else entry_px - atr_val * atr_mult_target

                        # Ensure valid stops
                        if stop <= 0 or target <= 0 or abs(entry_px - stop) < 0.01:
                            continue

                        # Position sizing
                        risk_per_trade = portfolio * 0.02
                        stop_dist      = abs(entry_px - stop)
                        shares         = max(1, int(risk_per_trade / stop_dist))
                        max_shares     = max(1, int((portfolio * 0.15) / entry_px))
                        shares         = min(shares, max_shares)

                        in_trade   = True
                        entry_date = bar_date

                else:
                    next_high  = df['high'].iloc[i+1]
                    next_low   = df['low'].iloc[i+1]
                    next_close = df['close'].iloc[i+1]

                    hit_stop   = next_low  <= stop   if direction == 'long' else next_high >= stop
                    hit_target = next_high >= target if direction == 'long' else next_low  <= target

                    # Also exit after 20 bars if neither hit
                    bars_held  = i - list(df.index).index(entry_date) if entry_date in df.index else 0
                    time_exit  = bars_held >= 20

                    if hit_stop or hit_target or time_exit:
                        if hit_target:
                            exit_px    = target
                            exit_reason = "TARGET"
                        elif hit_stop:
                            exit_px    = stop
                            exit_reason = "STOP"
                        else:
                            exit_px    = next_close
                            exit_reason = "TIME"

                        pnl_per_share = (exit_px - entry_px) if direction == 'long' else (entry_px - exit_px)
                        pnl  = pnl_per_share * shares
                        cost = entry_px * shares * 0.0014
                        pnl -= cost

                        portfolio += pnl
                        result     = "WIN" if pnl > 0 else "LOSS"

                        all_trades.append({
                            "symbol":      symbol,
                            "direction":   direction,
                            "entry_date":  str(entry_date.date() if hasattr(entry_date, 'date') else entry_date),
                            "exit_date":   str(bar_date.date() if hasattr(bar_date, 'date') else bar_date),
                            "entry_price": round(entry_px, 4),
                            "exit_price":  round(exit_px, 4),
                            "shares":      shares,
                            "pnl":         round(pnl, 2),
                            "pnl_pct":     round(pnl_per_share / (entry_px + 1e-9) * 100, 3),
                            "result":      result,
                            "exit_reason": exit_reason,
                            "portfolio":   round(portfolio, 2),
                            "cost":        round(cost, 2),
                        })

                        equity_curve.append(round(portfolio, 2))
                        in_trade = False

            if all_trades:
                sym_trades = [t for t in all_trades if t['symbol'] == symbol]
                if sym_trades:
                    wins = sum(1 for t in sym_trades if t['result'] == 'WIN')
                    logger.info(f"  {symbol}: {len(sym_trades)} trades, "
                               f"{wins/len(sym_trades):.0%} win rate, "
                               f"P&L ${sum(t['pnl'] for t in sym_trades):+.2f}")

        except Exception as e:
            logger.error(f"Backtest error for {symbol}: {e}", exc_info=True)

    return {
        "trades":          all_trades,
        "equity_curve":    equity_curve,
        "final_portfolio": portfolio,
        "days":            days,
    }


# =============================================================================
# STATISTICS ENGINE
# =============================================================================

def calculate_statistics(results: dict) -> dict:
    """Calculate comprehensive performance statistics."""
    trades  = results["trades"]
    equity  = results["equity_curve"]

    if not trades:
        return {"error": "No trades generated"}

    pnls    = [t["pnl"] for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    returns = pd.Series(pnls) / config.PORTFOLIO_SIZE

    win_rate        = len(wins) / len(pnls)
    avg_win         = np.mean(wins) if wins else 0
    avg_loss        = np.mean(losses) if losses else 0
    profit_factor   = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
    total_return    = (results["final_portfolio"] - config.PORTFOLIO_SIZE) / config.PORTFOLIO_SIZE
    total_pnl       = sum(pnls)

    # Risk metrics
    daily_returns   = returns
    sharpe          = float(daily_returns.mean() / (daily_returns.std() + 1e-9) * np.sqrt(252)) if len(daily_returns) > 1 else 0
    downside        = daily_returns[daily_returns < 0]
    sortino         = float(daily_returns.mean() / (downside.std() + 1e-9) * np.sqrt(252)) if len(downside) > 1 else 0

    # Max drawdown
    eq              = pd.Series(equity)
    rolling_max     = eq.expanding().max()
    drawdown        = (eq - rolling_max) / rolling_max
    max_dd          = float(drawdown.min())
    max_dd_dollar   = float((eq - rolling_max).min())

    calmar          = total_return / abs(max_dd) if max_dd != 0 else 0

    # Consecutive wins/losses
    results_list    = [t["result"] for t in trades]
    max_consec_wins = max_consecutive(results_list, "WIN")
    max_consec_loss = max_consecutive(results_list, "LOSS")

    # Per-symbol breakdown
    symbol_stats    = {}
    for trade in trades:
        sym = trade["symbol"]
        if sym not in symbol_stats:
            symbol_stats[sym] = {"trades": 0, "wins": 0, "pnl": 0}
        symbol_stats[sym]["trades"] += 1
        symbol_stats[sym]["pnl"]    += trade["pnl"]
        if trade["result"] == "WIN":
            symbol_stats[sym]["wins"] += 1

    for sym in symbol_stats:
        s = symbol_stats[sym]
        s["win_rate"] = s["wins"] / s["trades"] if s["trades"] > 0 else 0

    # Monthly returns
    monthly = {}
    for trade in trades:
        month = trade["exit_date"][:7]  # YYYY-MM
        monthly[month] = monthly.get(month, 0) + trade["pnl"]

    return {
        "total_trades":     len(trades),
        "win_rate":         win_rate,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "profit_factor":    profit_factor,
        "total_pnl":        total_pnl,
        "total_return_pct": total_return * 100,
        "final_portfolio":  results["final_portfolio"],
        "sharpe_ratio":     sharpe,
        "sortino_ratio":    sortino,
        "calmar_ratio":     calmar,
        "max_drawdown_pct": max_dd * 100,
        "max_drawdown_dollar": max_dd_dollar,
        "max_consec_wins":  max_consec_wins,
        "max_consec_losses":max_consec_loss,
        "expectancy":       (win_rate * avg_win + (1-win_rate) * avg_loss),
        "symbol_stats":     symbol_stats,
        "monthly_pnl":      monthly,
        "equity_curve":     equity,
        "days_tested":      results["days"],
    }


def max_consecutive(lst: list, value: str) -> int:
    """Count maximum consecutive occurrences of value in list."""
    max_count = count = 0
    for item in lst:
        count = count + 1 if item == value else 0
        max_count = max(max_count, count)
    return max_count


# =============================================================================
# HTML REPORT GENERATOR
# =============================================================================

def generate_html_report(stats: dict, trades: list, output_file: str = "backtest_report.html"):
    """Generate a beautiful HTML report with all statistics."""

    # Equity curve for chart
    equity_data = json.dumps(stats.get("equity_curve", [config.PORTFOLIO_SIZE]))

    # Monthly PnL data
    monthly = stats.get("monthly_pnl", {})
    monthly_labels = json.dumps(list(monthly.keys()))
    monthly_values = json.dumps([round(v, 2) for v in monthly.values()])

    # Per-symbol data
    sym_stats = stats.get("symbol_stats", {})
    sym_labels = json.dumps(list(sym_stats.keys()))
    sym_pnl    = json.dumps([round(sym_stats[s]["pnl"], 2) for s in sym_stats])
    sym_wr     = json.dumps([round(sym_stats[s]["win_rate"] * 100, 1) for s in sym_stats])

    # Recent trades table
    recent_trades = trades[-30:] if trades else []
    trades_rows = ""
    for t in reversed(recent_trades):
        color = "#00d4aa" if t["result"] == "WIN" else "#ff4757"
        trades_rows += f"""
        <tr>
            <td>{t['exit_date']}</td>
            <td>{t['symbol']}</td>
            <td>{t['direction'].upper()}</td>
            <td>${t['entry_price']:.2f}</td>
            <td>${t['exit_price']:.2f}</td>
            <td>{t['shares']}</td>
            <td style="color:{color};font-weight:bold">${t['pnl']:+.2f}</td>
            <td style="color:{color}">{t['pnl_pct']:+.2f}%</td>
            <td>{t['exit_reason']}</td>
        </tr>"""

    # Score card color
    def score_color(val, good_high=True):
        if good_high:
            return "#00d4aa" if val > 0 else "#ff4757"
        return "#ff4757" if val > 0 else "#00d4aa"

    sharpe_color  = "#00d4aa" if stats.get("sharpe_ratio", 0) > 1 else "#ff9f43"
    wr_color      = "#00d4aa" if stats.get("win_rate", 0) > 0.5 else "#ff4757"
    pnl_color     = score_color(stats.get("total_pnl", 0))
    dd_color      = "#ff9f43" if abs(stats.get("max_drawdown_pct", 0)) < 15 else "#ff4757"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Elite Trading Bot — Backtest Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&display=swap');
  :root {{
    --bg:#0a0c0f; --surface:#111318; --surface2:#1a1d24;
    --border:#1f2330; --accent:#00d4aa; --accent2:#3d7eff;
    --warn:#ff9f43; --danger:#ff4757; --text:#e8eaf0; --muted:#6b7280;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; padding:30px 20px; }}
  body::before {{ content:''; position:fixed; inset:0;
    background-image:linear-gradient(rgba(0,212,170,.03) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,212,170,.03) 1px,transparent 1px);
    background-size:40px 40px; pointer-events:none; z-index:0; }}
  .container {{ max-width:1100px; margin:0 auto; position:relative; z-index:1; }}
  .header {{ text-align:center; padding:40px; background:linear-gradient(135deg,rgba(0,212,170,.08),rgba(61,126,255,.08));
    border:1px solid var(--border); border-radius:16px; margin-bottom:30px; }}
  .header h1 {{ font-size:36px; font-weight:600; color:var(--text); }}
  .header h1 span {{ color:var(--accent); }}
  .header p {{ color:var(--muted); margin-top:8px; }}
  .badge {{ display:inline-block; background:rgba(0,212,170,.12); border:1px solid rgba(0,212,170,.3);
    color:var(--accent); font-family:'Space Mono',monospace; font-size:11px;
    padding:4px 12px; border-radius:20px; letter-spacing:2px; margin-bottom:12px; }}
  .grid-4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:24px; }}
  .grid-2 {{ display:grid; grid-template-columns:repeat(2,1fr); gap:16px; margin-bottom:24px; }}
  .grid-3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:24px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; }}
  .stat-card {{ text-align:center; }}
  .stat-value {{ font-family:'Space Mono',monospace; font-size:26px; font-weight:700; display:block; }}
  .stat-label {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; margin-top:6px; display:block; }}
  h2 {{ font-size:18px; font-weight:600; margin-bottom:16px; color:var(--text); }}
  h3 {{ font-size:14px; font-weight:600; color:var(--accent); text-transform:uppercase;
    letter-spacing:1px; margin-bottom:12px; }}
  .chart-container {{ position:relative; height:280px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:var(--surface2); color:var(--muted); font-weight:500;
    padding:10px 12px; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:1px; }}
  td {{ padding:9px 12px; border-bottom:1px solid var(--border); }}
  tr:hover td {{ background:rgba(255,255,255,.03); }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }}
  .tag-win {{ background:rgba(0,212,170,.15); color:var(--accent); }}
  .tag-loss {{ background:rgba(255,71,87,.15); color:var(--danger); }}
  @media(max-width:700px) {{ .grid-4,.grid-3 {{ grid-template-columns:1fr 1fr; }} }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <div class="badge">BACKTEST REPORT</div>
    <h1>Elite <span>Trading Bot</span></h1>
    <p>Last {stats.get('days_tested', 90)} days | Portfolio: ${config.PORTFOLIO_SIZE:,.2f} | 
       Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
  </div>

  <!-- KEY METRICS -->
  <div class="grid-4">
    <div class="card stat-card">
      <span class="stat-value" style="color:{pnl_color}">${stats.get('total_pnl', 0):+,.2f}</span>
      <span class="stat-label">Total P&L</span>
    </div>
    <div class="card stat-card">
      <span class="stat-value" style="color:{pnl_color}">{stats.get('total_return_pct', 0):+.1f}%</span>
      <span class="stat-label">Total Return</span>
    </div>
    <div class="card stat-card">
      <span class="stat-value" style="color:{wr_color}">{stats.get('win_rate', 0)*100:.1f}%</span>
      <span class="stat-label">Win Rate</span>
    </div>
    <div class="card stat-card">
      <span class="stat-value">{stats.get('total_trades', 0)}</span>
      <span class="stat-label">Total Trades</span>
    </div>
  </div>

  <div class="grid-4">
    <div class="card stat-card">
      <span class="stat-value" style="color:{sharpe_color}">{stats.get('sharpe_ratio', 0):.2f}</span>
      <span class="stat-label">Sharpe Ratio</span>
    </div>
    <div class="card stat-card">
      <span class="stat-value" style="color:{sharpe_color}">{stats.get('sortino_ratio', 0):.2f}</span>
      <span class="stat-label">Sortino Ratio</span>
    </div>
    <div class="card stat-card">
      <span class="stat-value" style="color:{dd_color}">{stats.get('max_drawdown_pct', 0):.1f}%</span>
      <span class="stat-label">Max Drawdown</span>
    </div>
    <div class="card stat-card">
      <span class="stat-value">{stats.get('profit_factor', 0):.2f}x</span>
      <span class="stat-label">Profit Factor</span>
    </div>
  </div>

  <!-- CHARTS -->
  <div class="grid-2">
    <div class="card">
      <h3>Equity Curve</h3>
      <div class="chart-container">
        <canvas id="equityChart"></canvas>
      </div>
    </div>
    <div class="card">
      <h3>Monthly P&L</h3>
      <div class="chart-container">
        <canvas id="monthlyChart"></canvas>
      </div>
    </div>
  </div>

  <!-- SYMBOL BREAKDOWN -->
  <div class="card" style="margin-bottom:24px">
    <h3>Per-Symbol Performance</h3>
    <div class="chart-container" style="height:220px">
      <canvas id="symbolChart"></canvas>
    </div>
  </div>

  <!-- DETAILED STATS -->
  <div class="grid-3" style="margin-bottom:24px">
    <div class="card">
      <h3>Trade Stats</h3>
      <table>
        <tr><td>Avg Win</td><td style="text-align:right;color:var(--accent)">${stats.get('avg_win', 0):+.2f}</td></tr>
        <tr><td>Avg Loss</td><td style="text-align:right;color:var(--danger)">${stats.get('avg_loss', 0):+.2f}</td></tr>
        <tr><td>Expectancy</td><td style="text-align:right">${stats.get('expectancy', 0):.2f}</td></tr>
        <tr><td>Max Consec Wins</td><td style="text-align:right;color:var(--accent)">{stats.get('max_consec_wins', 0)}</td></tr>
        <tr><td>Max Consec Losses</td><td style="text-align:right;color:var(--danger)">{stats.get('max_consec_losses', 0)}</td></tr>
      </table>
    </div>
    <div class="card">
      <h3>Risk Metrics</h3>
      <table>
        <tr><td>Sharpe Ratio</td><td style="text-align:right">{stats.get('sharpe_ratio', 0):.3f}</td></tr>
        <tr><td>Sortino Ratio</td><td style="text-align:right">{stats.get('sortino_ratio', 0):.3f}</td></tr>
        <tr><td>Calmar Ratio</td><td style="text-align:right">{stats.get('calmar_ratio', 0):.3f}</td></tr>
        <tr><td>Max DD ($)</td><td style="text-align:right;color:var(--danger)">${stats.get('max_drawdown_dollar', 0):.2f}</td></tr>
        <tr><td>Final Portfolio</td><td style="text-align:right">${stats.get('final_portfolio', 0):,.2f}</td></tr>
      </table>
    </div>
    <div class="card">
      <h3>Grade</h3>
      <table>
        <tr><td>Sharpe Target</td><td style="text-align:right">{'PASS' if stats.get('sharpe_ratio',0)>1 else 'FAIL'}</td></tr>
        <tr><td>Win Rate Target</td><td style="text-align:right">{'PASS' if stats.get('win_rate',0)>0.5 else 'FAIL'}</td></tr>
        <tr><td>Profit Factor</td><td style="text-align:right">{'PASS' if stats.get('profit_factor',0)>1.3 else 'FAIL'}</td></tr>
        <tr><td>Max DD Target</td><td style="text-align:right">{'PASS' if abs(stats.get('max_drawdown_pct',0))<15 else 'FAIL'}</td></tr>
        <tr><td>Profitable</td><td style="text-align:right">{'PASS' if stats.get('total_pnl',0)>0 else 'FAIL'}</td></tr>
      </table>
    </div>
  </div>

  <!-- TRADE LOG -->
  <div class="card">
    <h3>Recent Trades (last 30)</h3>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr><th>Date</th><th>Symbol</th><th>Dir</th><th>Entry</th><th>Exit</th>
              <th>Shares</th><th>P&L</th><th>%</th><th>Reason</th></tr>
        </thead>
        <tbody>{trades_rows}</tbody>
      </table>
    </div>
  </div>

</div>

<script>
const accent = '#00d4aa', danger = '#ff4757', warn = '#ff9f43', muted = '#6b7280';
const gridColor = 'rgba(31,35,48,0.8)';
const defaults = {{
  color: '#e8eaf0',
  plugins: {{ legend: {{ display: false }},
    tooltip: {{ backgroundColor: '#111318', borderColor: '#1f2330', borderWidth: 1 }} }},
  scales: {{
    x: {{ grid: {{ color: gridColor }}, ticks: {{ color: muted }} }},
    y: {{ grid: {{ color: gridColor }}, ticks: {{ color: muted }} }}
  }}
}};

// Equity Curve
const equityData = {equity_data};
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: equityData.map((_,i) => i),
    datasets: [{{
      data: equityData,
      borderColor: equityData[equityData.length-1] >= equityData[0] ? accent : danger,
      backgroundColor: equityData[equityData.length-1] >= equityData[0]
        ? 'rgba(0,212,170,0.08)' : 'rgba(255,71,87,0.08)',
      fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2
    }}]
  }},
  options: {{ ...defaults, responsive:true, maintainAspectRatio:false }}
}});

// Monthly P&L
const mLabels = {monthly_labels};
const mValues = {monthly_values};
new Chart(document.getElementById('monthlyChart'), {{
  type: 'bar',
  data: {{
    labels: mLabels,
    datasets: [{{
      data: mValues,
      backgroundColor: mValues.map(v => v >= 0 ? 'rgba(0,212,170,0.7)' : 'rgba(255,71,87,0.7)'),
      borderRadius: 4
    }}]
  }},
  options: {{ ...defaults, responsive:true, maintainAspectRatio:false }}
}});

// Symbol P&L
new Chart(document.getElementById('symbolChart'), {{
  type: 'bar',
  data: {{
    labels: {sym_labels},
    datasets: [{{
      label: 'P&L ($)',
      data: {sym_pnl},
      backgroundColor: {sym_pnl}.map(v => v >= 0 ? 'rgba(0,212,170,0.7)' : 'rgba(255,71,87,0.7)'),
      borderRadius: 4
    }}]
  }},
  options: {{ ...defaults, responsive:true, maintainAspectRatio:false }}
}});
</script>
</body>
</html>"""

    with open(output_file, 'w') as f:
        f.write(html)
    logger.info(f"Report saved: {output_file}")
    return output_file


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Elite Backtest Report')
    parser.add_argument('--days', type=int, default=90, help='Days to backtest')
    parser.add_argument('--output', default='backtest_report.html', help='Output file')
    args = parser.parse_args()

    logger.info("="*60)
    logger.info("ELITE BACKTESTING ENGINE")
    logger.info(f"  Days: {args.days} | Symbols: {len(config.WATCHLIST)}")
    logger.info(f"  Portfolio: ${config.PORTFOLIO_SIZE:,.2f}")
    logger.info("="*60)

    # Connect to Alpaca for data
    from main import connect_alpaca
    trading_client, data_client = connect_alpaca()

    # Run backtest
    logger.info("Running backtest...")
    results = run_backtest(data_client, args.days)

    if not results["trades"]:
        logger.warning("No trades generated. Try a longer timeframe or lower MIN_SIGNAL_SCORE in config.py")
        return

    # Calculate stats
    logger.info("Calculating statistics...")
    stats = calculate_statistics(results)

    # Print summary to terminal
    logger.info("\n" + "="*60)
    logger.info("BACKTEST RESULTS")
    logger.info("="*60)
    logger.info(f"  Total Trades:   {stats['total_trades']}")
    logger.info(f"  Win Rate:       {stats['win_rate']:.1%}")
    logger.info(f"  Total P&L:      ${stats['total_pnl']:+,.2f}")
    logger.info(f"  Total Return:   {stats['total_return_pct']:+.1f}%")
    logger.info(f"  Sharpe Ratio:   {stats['sharpe_ratio']:.2f}")
    logger.info(f"  Max Drawdown:   {stats['max_drawdown_pct']:.1f}%")
    logger.info(f"  Profit Factor:  {stats['profit_factor']:.2f}x")
    logger.info("="*60)

    # Generate HTML report
    logger.info("Generating HTML report...")
    output = generate_html_report(stats, results["trades"], args.output)

    # Open in browser
    import webbrowser
    webbrowser.open(f"file:///{os.path.abspath(output)}")
    logger.info(f"Report opened in browser: {output}")


if __name__ == "__main__":
    main()
