"""
bars_fetch.py — Shared yfinance daily-bar fetcher for Ares
=============================================================
Single canonical implementation of the OHLCV bar-fetch routine used across
all engines and monitors. Created 2026-06-28 to consolidate 7 independent
copies of this exact function that had drifted (one, ares_hold_monitor.py,
was still on the old Alpaca/IEX endpoint — same failure mode as RF-37: a
fix applied everywhere except one file, silently degrading that file).

Replaces Alpaca/IEX bar endpoint which only covers ~2-3% of consolidated
tape on paper accounts, causing 60-80% of universe symbols to return no
data. yfinance covers all symbols with 5+ years of history at no cost.
Order submission remains on Alpaca (unaffected) — this module is bar
data only.

Callers: engine_a.py, engine_b.py, engine_c.py, engine_e.py, engine_f.py,
ares_hold_monitor.py, ares_exit_monitor.py.

Each caller keeps its own thin local wrapper (same function name/signature
it had before) so call sites don't need to change — the wrapper just pins
that file's lookback default and min_bars threshold and delegates here.
Any future fix to the fetch logic itself only needs to happen in this file.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List

import pandas as pd

logger = logging.getLogger("ares.bars_fetch")


def fetch_bars(symbols: List[str], lookback: int = 120, min_bars: int = 5,
                batch_size: int = 100) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance, batched in groups of `batch_size`.

    Returns {symbol: DataFrame} where each DataFrame has lowercase
    open/high/low/close/volume columns plus a `timestamp` column, tail-
    clipped to `lookback` rows. A symbol is included only if it has at
    least `min_bars` valid rows after the fetch.
    """
    import yfinance as yf

    if not symbols:
        return {}

    end = datetime.now()
    start = end - timedelta(days=int(lookback * 1.6))
    bars_out: Dict[str, pd.DataFrame] = {}

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
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
                        if len(df) >= min_bars:
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
                if len(df) >= min_bars:
                    bars_out[sym] = df

        except Exception as e:
            logger.warning("yfinance bar fetch failed (batch %d): %s", i // batch_size, e)

    return bars_out
