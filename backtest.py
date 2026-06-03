"""
Raptor v5.2 — Backtester
=========================
Simulates the QuantSignalEngine against historical data.

Execution model:
  - Daily: score entire universe cross-sectionally
  - Entry: next-day open + slippage on signals with t > 1.65
  - Exit: ATR stops (initial 1.5x, trail 1.0x), TP (3.0x ATR), time stop (30d)
  - Sizing: Kelly fraction from signal strength, regime-adjusted
  - No lookahead bias: signals use data up to day T, execution at T+1 open

Usage:
  python backtest.py                    # Standard run
  python backtest.py --start 2022-01-01 # Custom start
  python backtest.py --end 2024-12-31   # Custom end
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import RaptorConfig, CONFIG
from signals import QuantSignalEngine, Factors, Signal, MIN_BARS_REQUIRED

logger = logging.getLogger("raptor.backtest")


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    hold_days: int
    exit_reason: str
    t_statistic: float
    composite_score: float
    kelly_fraction: float
    regime: str


@dataclass
class Position:
    symbol: str
    entry_date: str
    entry_price: float
    shares: int
    stop_price: float
    take_profit: float
    trailing_stop: float
    high_water: float
    t_statistic: float
    composite_score: float
    kelly_fraction: float
    regime: str
    days_held: int = 0


class Backtester:
    """
    Daily walk-through backtester.
    Each day: check exits on open positions → generate new signals → execute entries.
    """

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.rcfg = cfg.risk
        self.bcfg = cfg.backtest
        self.engine = QuantSignalEngine(cfg)
        self.factors = Factors()

    def _load_data(self, symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
        """Load historical bars via Alpaca. Cache to parquet."""
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        cache_dir = os.path.join("cache", "backtest_bars")
        os.makedirs(cache_dir, exist_ok=True)

        client = StockHistoricalDataClient(
            api_key=self.cfg.alpaca.api_key,
            secret_key=self.cfg.alpaca.secret_key,
        )

        # Fetch extra history for warmup (need MIN_BARS_REQUIRED before start)
        warmup_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")

        results = {}
        for symbol in symbols:
            cache_file = os.path.join(cache_dir, f"{symbol}_{warmup_start}_{end}.parquet")

            if os.path.exists(cache_file):
                df = pd.read_parquet(cache_file)
                if len(df) >= MIN_BARS_REQUIRED:
                    results[symbol] = df
                    continue

            try:
                request = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=datetime.strptime(warmup_start, "%Y-%m-%d"),
                    end=datetime.strptime(end, "%Y-%m-%d"),
                    feed=self.cfg.alpaca.feed,
                )
                bars = client.get_stock_bars(request)
                if symbol in bars.data:
                    rows = [{
                        "timestamp": b.timestamp, "open": float(b.open),
                        "high": float(b.high), "low": float(b.low),
                        "close": float(b.close), "volume": int(b.volume),
                        "vwap": float(b.vwap) if b.vwap else np.nan,
                    } for b in bars.data[symbol]]

                    df = pd.DataFrame(rows)
                    df["timestamp"] = pd.to_datetime(df["timestamp"])
                    df = df.set_index("timestamp").sort_index()

                    if len(df) >= MIN_BARS_REQUIRED:
                        df.to_parquet(cache_file)
                        results[symbol] = df
                        logger.info("Loaded %s: %d bars", symbol, len(df))
            except Exception as e:
                logger.warning("Failed to load %s: %s", symbol, e)

        logger.info("Loaded %d / %d symbols", len(results), len(symbols))
        return results

    def _slippage(self, price: float, side: str) -> float:
        slip = self.bcfg.slippage_bps / 10_000
        return price * (1 + slip) if side == "BUY" else price * (1 - slip)

    def _check_exits(self, date, positions, bars_dict):
        """
        Time-dependent trailing stop (Bertsimas & Lo 1998).

        For mean-reverting processes, the optimal exit boundary is
        time-dependent: wide early (survive setup noise), then
        narrows as the expected reversion time approaches.

        No fixed take-profit. Winners run until the trail catches them.
        """
        remaining, closed = [], []

        for pos in positions:
            pos.days_held += 1

            if pos.symbol not in bars_dict:
                remaining.append(pos)
                continue

            df = bars_dict[pos.symbol]
            day_data = df.loc[df.index <= date]
            if len(day_data) == 0:
                remaining.append(pos)
                continue

            today = day_data.iloc[-1]
            hi, lo, cl = today["high"], today["low"], today["close"]

            exit_price, reason = None, None

            # Stop loss check
            if lo <= pos.stop_price:
                exit_price = self._slippage(pos.stop_price, "SELL")
                reason = "stop_loss"
            # Time stop
            elif pos.days_held >= self.rcfg.time_stop_days:
                exit_price = self._slippage(cl, "SELL")
                reason = "time_stop"

            if exit_price is not None:
                pnl = (exit_price - pos.entry_price) * pos.shares
                pnl_pct = exit_price / pos.entry_price - 1
                closed.append(Trade(
                    symbol=pos.symbol, entry_date=pos.entry_date,
                    exit_date=str(date.date()), entry_price=pos.entry_price,
                    exit_price=round(exit_price, 2), shares=pos.shares,
                    pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
                    hold_days=pos.days_held, exit_reason=reason,
                    t_statistic=pos.t_statistic, composite_score=pos.composite_score,
                    kelly_fraction=pos.kelly_fraction, regime=pos.regime,
                ))
            else:
                # Update high water mark
                if hi > pos.high_water:
                    pos.high_water = hi

                # Time-dependent trail multiplier (Bertsimas & Lo)
                lookback = day_data.tail(self.rcfg.atr_period + 1)
                if len(lookback) > self.rcfg.atr_period:
                    atr = self.factors.atr(lookback, self.rcfg.atr_period)
                    if atr > 0:
                        # Select trail width based on days held
                        if pos.days_held <= self.rcfg.trail_early_days:
                            trail_mult = self.rcfg.trail_early_atr
                        elif pos.days_held <= self.rcfg.trail_mid_days:
                            trail_mult = self.rcfg.trail_mid_atr
                        elif pos.days_held <= self.rcfg.trail_late_days:
                            trail_mult = self.rcfg.trail_late_atr
                        else:
                            trail_mult = self.rcfg.trail_final_atr

                        new_trail = pos.high_water - trail_mult * atr
                        # Trail only ratchets up, never down
                        pos.trailing_stop = max(pos.trailing_stop, new_trail)
                        pos.stop_price = max(pos.stop_price, pos.trailing_stop)

                remaining.append(pos)

        return remaining, closed

    def run(self, symbols=None, start=None, end=None):
        """Run full backtest simulation."""
        start = start or self.bcfg.start_date
        end = end or self.bcfg.end_date

        if not symbols:
            symbols = [
                "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
                "AMD", "CRM", "NFLX", "ADBE", "PYPL", "SQ", "SHOP",
                "UBER", "ABNB", "COIN", "SNOW", "DDOG", "NET",
                "JPM", "BAC", "GS", "MS", "V", "MA",
                "XOM", "CVX", "LLY", "UNH", "JNJ", "PFE",
                "CAT", "DE", "BA", "RTX", "LMT", "GE",
                "HD", "LOW", "TGT", "WMT", "COST", "NKE",
                "DIS", "CMCSA",
            ]

        print(f"Loading data for {len(symbols)} symbols ({start} to {end})...")
        all_bars = self._load_data(symbols, start, end)

        # SPY for relative strength
        if "SPY" not in all_bars:
            spy_bars_dict = self._load_data(["SPY"], start, end)
            if "SPY" in spy_bars_dict:
                all_bars["SPY"] = spy_bars_dict["SPY"]

        spy_full = all_bars.get("SPY")
        if spy_full is None:
            print("ERROR: Could not load SPY data")
            return None

        # Get all trading dates from SPY within the test period
        start_dt = pd.Timestamp(start, tz="UTC") if spy_full.index.tz else pd.Timestamp(start)
        end_dt = pd.Timestamp(end, tz="UTC") if spy_full.index.tz else pd.Timestamp(end)
        all_dates = spy_full.index[(spy_full.index >= start_dt) & (spy_full.index <= end_dt)]

        if len(all_dates) == 0:
            print("ERROR: No trading dates in range")
            return None

        print(f"Simulating {len(all_dates)} trading days with {len(all_bars)} symbols...")

        # State
        equity = self.bcfg.initial_capital
        cash = equity
        positions: List[Position] = []
        all_trades: List[Trade] = []
        equity_curve = []
        date_list = []

        # Fake macro for backtest (neutral baseline)
        macro = {"score": 0.0, "regime": "NEUTRAL", "snapshot": {}}

        for day_idx, date in enumerate(all_dates):
            # 1. Check exits
            positions, closed = self._check_exits(date, positions, all_bars)
            for t in closed:
                cash += t.exit_price * t.shares
                all_trades.append(t)

            # 2. Generate signals (cross-sectional scoring)
            # Build lookback windows ending at current date
            current_bars = {}
            for sym, df in all_bars.items():
                if sym == "SPY":
                    continue
                window = df.loc[df.index <= date].tail(MIN_BARS_REQUIRED + 20)
                if len(window) >= MIN_BARS_REQUIRED:
                    current_bars[sym] = window

            spy_window = None
            if spy_full is not None:
                spy_window = spy_full.loc[spy_full.index <= date].tail(MIN_BARS_REQUIRED + 20)
                if len(spy_window) < MIN_BARS_REQUIRED:
                    spy_window = None

            if len(current_bars) >= 10:
                # Fake sentiment (neutral in backtest)
                fake_sent = {s: {"score": 0.0} for s in current_bars}

                signals = self.engine.generate_signals(
                    current_bars, macro, fake_sent, spy_window
                )

                # Filter held symbols
                held = {p.symbol for p in positions}
                signals = [s for s in signals if s.symbol not in held]

                # Execute entries
                for sig in signals:
                    if len(positions) >= self.rcfg.max_positions:
                        break

                    entry = self._slippage(sig.entry_price, "BUY")
                    cost = entry * int((equity * sig.kelly_fraction) / entry)
                    shares = int((equity * sig.kelly_fraction) / entry)

                    if shares < 1 or cost > cash * 0.95:
                        continue

                    cash -= entry * shares
                    positions.append(Position(
                        symbol=sig.symbol, entry_date=str(date.date()),
                        entry_price=round(entry, 2), shares=shares,
                        stop_price=sig.stop_price, take_profit=sig.take_profit,
                        trailing_stop=sig.stop_price, high_water=entry,
                        t_statistic=sig.t_statistic,
                        composite_score=sig.composite_score,
                        kelly_fraction=sig.kelly_fraction, regime=sig.regime,
                    ))

            # 3. Mark-to-market
            pos_value = 0
            for pos in positions:
                if pos.symbol in all_bars:
                    sym_data = all_bars[pos.symbol].loc[all_bars[pos.symbol].index <= date]
                    if len(sym_data) > 0:
                        pos_value += sym_data["close"].iloc[-1] * pos.shares

            equity = cash + pos_value
            equity_curve.append(equity)
            date_list.append(date)

            # Progress
            if (day_idx + 1) % 100 == 0:
                print(f"  Day {day_idx+1}/{len(all_dates)}  equity=${equity:,.0f}  "
                      f"positions={len(positions)}  trades={len(all_trades)}")

        # Build results
        eq = pd.Series(equity_curve, index=date_list)

        # Benchmark
        spy_test = spy_full.loc[(spy_full.index >= start_dt) & (spy_full.index <= end_dt)]
        bench = pd.Series(dtype=float)
        if len(spy_test) > 0:
            bench = (spy_test["close"] / spy_test["close"].iloc[0]) * self.bcfg.initial_capital

        daily_ret = eq.pct_change().dropna()
        metrics = self._compute_metrics(all_trades, eq, daily_ret, bench)

        return {"trades": all_trades, "equity": eq, "benchmark": bench,
                "daily_returns": daily_ret, "metrics": metrics}

    def _compute_metrics(self, trades, equity, daily_ret, benchmark):
        if not trades:
            return {"error": "No trades"}

        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        total_ret = equity.iloc[-1] / equity.iloc[0] - 1
        days = len(daily_ret)
        ann = 252 / max(days, 1)

        # Sharpe
        excess = daily_ret - self.bcfg.risk_free_rate / 252
        sharpe = np.sqrt(252) * excess.mean() / excess.std() if excess.std() > 0 else 0

        # Sortino
        down = daily_ret[daily_ret < 0]
        sortino = np.sqrt(252) * daily_ret.mean() / down.std() if len(down) > 0 and down.std() > 0 else 0

        # Max DD
        peak = equity.expanding().max()
        dd = (equity - peak) / peak
        max_dd = dd.min()

        # CAGR
        cagr = (1 + total_ret) ** ann - 1 if total_ret > -1 else 0
        calmar = abs(cagr / max_dd) if max_dd != 0 else 0

        # Win rate
        wr = len(winners) / len(trades) if trades else 0
        avg_win = np.mean([t.pnl_pct for t in winners]) if winners else 0
        avg_loss = np.mean([t.pnl_pct for t in losers]) if losers else 0
        expectancy = wr * avg_win + (1 - wr) * avg_loss

        # Profit factor
        gross_w = sum(t.pnl for t in winners)
        gross_l = abs(sum(t.pnl for t in losers))
        pf = gross_w / gross_l if gross_l > 0 else float("inf")

        # Holds
        holds = [t.hold_days for t in trades]

        # Exit reasons
        exits = {}
        for t in trades:
            exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1

        # Benchmark
        bench_ret = (benchmark.iloc[-1] / benchmark.iloc[0] - 1) if len(benchmark) > 1 else 0

        return {
            "total_return_pct": round(total_ret * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "benchmark_return_pct": round(bench_ret * 100, 2),
            "alpha_pct": round((total_ret - bench_ret) * 100, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "calmar": round(calmar, 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "annual_vol_pct": round(daily_ret.std() * np.sqrt(252) * 100, 2),
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate_pct": round(wr * 100, 1),
            "avg_win_pct": round(avg_win * 100, 2),
            "avg_loss_pct": round(avg_loss * 100, 2),
            "expectancy_pct": round(expectancy * 100, 3),
            "profit_factor": round(pf, 2),
            "avg_hold_days": round(np.mean(holds), 1) if holds else 0,
            "median_hold_days": round(np.median(holds), 1) if holds else 0,
            "exit_reasons": exits,
            "net_pnl": round(sum(t.pnl for t in trades), 2),
            "final_equity": round(equity.iloc[-1], 2),
            "initial_capital": self.bcfg.initial_capital,
        }

    def print_report(self, results):
        m = results["metrics"]
        if "error" in m:
            print(f"BACKTEST FAILED: {m['error']}")
            return

        r = []
        r.append("=" * 65)
        r.append(f"  RAPTOR v5.2 BACKTEST REPORT")
        r.append(f"  {self.bcfg.start_date} to {self.bcfg.end_date}")
        r.append("=" * 65)
        r.append("")
        r.append("  RETURNS")
        r.append(f"    Total Return:      {m['total_return_pct']:>8.2f}%")
        r.append(f"    CAGR:              {m['cagr_pct']:>8.2f}%")
        r.append(f"    Benchmark (SPY):   {m['benchmark_return_pct']:>8.2f}%")
        r.append(f"    Alpha:             {m['alpha_pct']:>8.2f}%")
        r.append("")
        r.append("  RISK")
        r.append(f"    Sharpe Ratio:      {m['sharpe']:>8.3f}")
        r.append(f"    Sortino Ratio:     {m['sortino']:>8.3f}")
        r.append(f"    Calmar Ratio:      {m['calmar']:>8.3f}")
        r.append(f"    Max Drawdown:      {m['max_drawdown_pct']:>8.2f}%")
        r.append(f"    Annual Volatility: {m['annual_vol_pct']:>8.2f}%")
        r.append("")
        r.append("  TRADES")
        r.append(f"    Total Trades:      {m['total_trades']:>8d}")
        r.append(f"    Win Rate:          {m['win_rate_pct']:>8.1f}%")
        r.append(f"    Avg Win:           {m['avg_win_pct']:>8.2f}%")
        r.append(f"    Avg Loss:          {m['avg_loss_pct']:>8.2f}%")
        r.append(f"    Expectancy:        {m['expectancy_pct']:>8.3f}%")
        r.append(f"    Profit Factor:     {m['profit_factor']:>8.2f}")
        r.append("")
        r.append("  EXECUTION")
        r.append(f"    Avg Hold Days:     {m['avg_hold_days']:>8.1f}")
        r.append(f"    Median Hold:       {m['median_hold_days']:>8.1f}")
        for reason, count in m.get("exit_reasons", {}).items():
            r.append(f"    Exit [{reason:12s}]: {count:>5d}")
        r.append("")
        r.append("  P&L")
        r.append(f"    Net P&L:           ${m['net_pnl']:>12,.2f}")
        r.append(f"    Final Equity:      ${m['final_equity']:>12,.2f}")
        r.append("")
        r.append("=" * 65)

        report = "\n".join(r)
        print(report)
        return report

    def save_results(self, results, output_dir=None):
        out = output_dir or self.cfg.log.backtest_results_dir
        os.makedirs(out, exist_ok=True)

        # Trades CSV
        if results["trades"]:
            rows = [{
                "symbol": t.symbol, "entry_date": t.entry_date,
                "exit_date": t.exit_date, "entry_price": t.entry_price,
                "exit_price": t.exit_price, "shares": t.shares,
                "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                "hold_days": t.hold_days, "exit_reason": t.exit_reason,
                "t_statistic": t.t_statistic, "composite_score": t.composite_score,
                "kelly_fraction": t.kelly_fraction, "regime": t.regime,
            } for t in results["trades"]]
            pd.DataFrame(rows).to_csv(os.path.join(out, "trades.csv"), index=False)

        # Equity curve
        results["equity"].to_csv(os.path.join(out, "equity_curve.csv"))

        # Metrics
        with open(os.path.join(out, "metrics.json"), "w") as f:
            json.dump(results["metrics"], f, indent=2, default=str)

        # Report
        report = self.print_report(results)
        if report:
            with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
                f.write(report)

        print(f"Results saved to {out}/")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # Suppress noisy factor logging during backtest
    logging.getLogger("raptor.signals").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Raptor v5.2 Backtester")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    args = parser.parse_args()

    cfg = CONFIG
    try:
        cfg.validate_all()
    except AssertionError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    bt = Backtester(cfg)
    results = bt.run(start=args.start, end=args.end)

    if results:
        bt.print_report(results)
        bt.save_results(results)
    else:
        print("Backtest returned no results.")
