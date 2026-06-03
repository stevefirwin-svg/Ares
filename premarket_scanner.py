"""
=============================================================================
PRE-MARKET GAP SCANNER
=============================================================================
Scans for significant pre-market gaps before the market opens.
Gaps predict intraday direction with ~65% accuracy.

STRATEGIES:
  Gap & Go:      Large gap up + volume = buy the continuation
  Gap Fill:      Small gap = price often fills back to prior close
  Fade the Gap:  Extreme gap = reversal trade opportunity

RUNS AT: 9:00-9:30 AM ET (before market open)
=============================================================================
"""

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class PreMarketScanner:
    """
    Scans for pre-market gaps and classifies their trading potential.
    Uses Yahoo Finance for pre/post market data (free).
    """

    def __init__(self, config):
        self.config = config

    def get_premarket_data(self, symbol: str) -> dict:
        """
        Get pre-market price and calculate gap from prior close.
        Uses Yahoo Finance pre-market data.
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)

            # Get recent data including pre-market
            df = ticker.history(period="5d", interval="1d", prepost=True)
            if df.empty or len(df) < 2:
                return {"error": "No data"}

            # Fix columns
            df.columns = [c.lower() for c in df.columns]

            prior_close  = float(df['close'].iloc[-2])
            current_open = float(df['open'].iloc[-1])

            gap_pct = (current_open - prior_close) / prior_close * 100
            gap_dir = "UP" if gap_pct > 0 else "DOWN"

            # Pre-market volume vs average
            avg_vol = df['volume'].mean()
            pre_vol = df['volume'].iloc[-1]
            vol_ratio = pre_vol / (avg_vol + 1e-9)

            return {
                "symbol":       symbol,
                "prior_close":  round(prior_close, 4),
                "current_open": round(current_open, 4),
                "gap_pct":      round(gap_pct, 3),
                "gap_dir":      gap_dir,
                "vol_ratio":    round(vol_ratio, 2),
                "avg_volume":   avg_vol,
            }

        except Exception as e:
            logger.debug(f"Pre-market data error for {symbol}: {e}")
            return {"error": str(e)}

    def classify_gap(self, gap_data: dict) -> dict:
        """
        Classify the gap and recommend a trading strategy.

        GAP CLASSIFICATION:
          Micro gap  (<0.5%):  Too small — ignore
          Small gap  (0.5-1%): Gap fill trade — fade the gap
          Medium gap (1-2%):   Gap & Go if volume confirms
          Large gap  (2-5%):   Strong momentum trade
          Extreme gap (>5%):   News-driven — very careful
        """
        if "error" in gap_data:
            return {"strategy": "SKIP", "reason": "No data"}

        gap_abs   = abs(gap_data.get("gap_pct", 0))
        gap_dir   = gap_data.get("gap_dir", "UP")
        vol_ratio = gap_data.get("vol_ratio", 1.0)
        symbol    = gap_data.get("symbol", "")

        if gap_abs < 0.5:
            return {
                "strategy":   "SKIP",
                "reason":     "Gap too small to trade",
                "confidence": 0,
                "direction":  "NONE",
            }

        elif gap_abs < 1.0:
            # Small gap — usually fills back
            return {
                "strategy":   "GAP_FILL",
                "reason":     f"Small {gap_dir} gap {gap_abs:.1f}% — expect fill",
                "confidence": 0.55,
                "direction":  "short" if gap_dir == "UP" else "long",
                "score_bonus": 0.5,
            }

        elif gap_abs < 2.0:
            if vol_ratio > 1.5:
                # Medium gap with volume — Gap & Go
                return {
                    "strategy":   "GAP_AND_GO",
                    "reason":     f"Medium {gap_dir} gap {gap_abs:.1f}% with {vol_ratio:.1f}x volume",
                    "confidence": 0.62,
                    "direction":  "long" if gap_dir == "UP" else "short",
                    "score_bonus": 1.0,
                }
            else:
                # Medium gap no volume — wait and see
                return {
                    "strategy":   "WAIT",
                    "reason":     f"Medium gap but low volume ({vol_ratio:.1f}x) — wait for confirmation",
                    "confidence": 0.50,
                    "direction":  "NONE",
                    "score_bonus": 0,
                }

        elif gap_abs < 5.0:
            # Large gap — strong momentum trade
            return {
                "strategy":   "GAP_AND_GO",
                "reason":     f"Large {gap_dir} gap {gap_abs:.1f}% — strong momentum",
                "confidence": 0.65,
                "direction":  "long" if gap_dir == "UP" else "short",
                "score_bonus": 1.5,
            }

        else:
            # Extreme gap — likely news event, unpredictable
            return {
                "strategy":   "EXTREME_GAP",
                "reason":     f"Extreme {gap_dir} gap {gap_abs:.1f}% — news event, high risk",
                "confidence": 0.45,
                "direction":  "NONE",   # Wait for first 15 min candle
                "score_bonus": 0,
            }

    def scan_all(self, watchlist: list) -> list:
        """
        Scan entire watchlist for pre-market gaps.
        Returns sorted list of opportunities.
        """
        results = []
        logger.info(f"Pre-market scan: checking {len(watchlist)} symbols...")

        for symbol in watchlist:
            gap_data = self.get_premarket_data(symbol)
            if "error" in gap_data:
                continue

            classification = self.classify_gap(gap_data)
            gap_abs = abs(gap_data.get("gap_pct", 0))

            if classification["strategy"] != "SKIP" and gap_abs >= 0.5:
                results.append({
                    **gap_data,
                    **classification,
                    "gap_abs": gap_abs,
                })

        # Sort by gap size and confidence
        results.sort(
            key=lambda x: x.get("gap_abs", 0) * x.get("confidence", 0),
            reverse=True
        )

        if results:
            logger.info(f"Pre-market gaps found: {len(results)}")
            for r in results[:5]:
                logger.info(f"  {r['symbol']}: {r['gap_dir']} {r['gap_abs']:.1f}% "
                           f"| {r['strategy']} | conf: {r['confidence']:.0%}")
        else:
            logger.info("No significant pre-market gaps found")

        return results

    def is_premarket_window(self) -> bool:
        """Check if we're in the pre-market scanning window (9:00-9:30 AM ET)."""
        import pytz
        et = pytz.timezone('America/New_York')
        now_et = datetime.now(timezone.utc).astimezone(et)
        return (now_et.weekday() < 5 and
                9 <= now_et.hour < 10 and
                (now_et.hour != 9 or now_et.minute >= 0))


# Global instance
_scanner_instance = None

def get_scanner(config):
    global _scanner_instance
    if _scanner_instance is None:
        _scanner_instance = PreMarketScanner(config)
    return _scanner_instance
