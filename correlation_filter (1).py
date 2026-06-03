"""
=============================================================================
CORRELATION FILTER
=============================================================================
Prevents holding highly correlated positions simultaneously.

WHY THIS MATTERS:
If you hold NVDA + AMD + SMCI all at once and AI stocks crash,
all 3 positions lose at the same time — your diversification is fake.

The correlation filter ensures your open positions are genuinely
diversified so one market event doesn't wipe out everything.

HOW IT WORKS:
  - Calculates rolling 60-day correlation between all pairs
  - If new signal is >0.75 correlated with existing position: skip it
  - Prefers the BEST signal from each correlated group
=============================================================================
"""

import numpy as np
import pandas as pd
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum allowed correlation between any two open positions
MAX_CORRELATION = 0.75

# Known highly correlated pairs (override for speed, avoids live calculation)
KNOWN_HIGH_CORR = {
    # AI Semis — all move together
    frozenset(["NVDA", "AMD"]):   0.85,
    frozenset(["NVDA", "SMCI"]):  0.80,
    frozenset(["AMD",  "SMCI"]):  0.78,
    frozenset(["NVDA", "AVGO"]):  0.75,

    # Big Tech
    frozenset(["MSFT", "GOOGL"]): 0.80,
    frozenset(["META", "GOOGL"]): 0.78,
    frozenset(["AMZN", "MSFT"]):  0.76,

    # Energy pairs
    frozenset(["XOM",  "CVX"]):   0.90,
    frozenset(["COP",  "OXY"]):   0.85,
    frozenset(["SLB",  "HAL"]):   0.88,

    # Clean energy
    frozenset(["ENPH", "SEDG"]):  0.82,
    frozenset(["PLUG", "BE"]):    0.80,

    # ETF + components (ETF is redundant if holding components)
    frozenset(["QQQ",  "NVDA"]):  0.80,
    frozenset(["QQQ",  "MSFT"]):  0.78,
    frozenset(["XLE",  "XOM"]):   0.92,
    frozenset(["XLE",  "CVX"]):   0.90,
    frozenset(["SOXX", "NVDA"]):  0.88,
}


class CorrelationFilter:
    """
    Filters out new signals that are too correlated with existing positions.
    Protects against hidden concentration risk.
    """

    def __init__(self, config):
        self.config = config
        self._return_cache = {}   # Symbol -> recent returns cache

    def get_known_correlation(self, symbol1: str, symbol2: str) -> Optional[float]:
        """Check known correlation pairs (fast, no calculation needed)."""
        pair = frozenset([symbol1, symbol2])
        return KNOWN_HIGH_CORR.get(pair, None)

    def calculate_correlation(self, symbol1: str, symbol2: str,
                               returns_data: dict) -> float:
        """
        Calculate rolling correlation between two symbols.
        Uses pre-fetched returns data for speed.
        """
        try:
            # Check known pairs first
            known = self.get_known_correlation(symbol1, symbol2)
            if known is not None:
                return known

            # Calculate from returns data
            r1 = returns_data.get(symbol1)
            r2 = returns_data.get(symbol2)

            if r1 is None or r2 is None:
                return 0.5   # Unknown — assume moderate correlation

            # Align series
            aligned = pd.DataFrame({
                "r1": r1, "r2": r2
            }).dropna()

            if len(aligned) < 20:
                return 0.5

            corr = aligned['r1'].corr(aligned['r2'])
            return float(corr) if not np.isnan(corr) else 0.5

        except Exception as e:
            logger.debug(f"Correlation calc error: {e}")
            return 0.5

    def is_too_correlated(self, new_symbol: str,
                           open_positions: dict,
                           returns_data: dict = None) -> dict:
        """
        Check if new signal is too correlated with existing positions.
        Returns dict with decision and most correlated existing position.
        """
        if not open_positions:
            return {
                "too_correlated": False,
                "max_correlation": 0.0,
                "correlated_with": None,
                "action": "ALLOW"
            }

        if returns_data is None:
            returns_data = {}

        max_corr = 0.0
        most_correlated = None

        for existing_symbol in open_positions:
            if existing_symbol == new_symbol:
                continue

            corr = self.calculate_correlation(
                new_symbol, existing_symbol, returns_data
            )

            if corr > max_corr:
                max_corr = corr
                most_correlated = existing_symbol

        if max_corr >= MAX_CORRELATION:
            return {
                "too_correlated": True,
                "max_correlation": round(max_corr, 3),
                "correlated_with": most_correlated,
                "action":          "SKIP",
                "reason": (
                    f"{new_symbol} is {max_corr:.0%} correlated with "
                    f"existing position {most_correlated} — skipping to "
                    f"avoid concentration risk"
                )
            }

        return {
            "too_correlated": False,
            "max_correlation": round(max_corr, 3),
            "correlated_with": most_correlated,
            "action":          "ALLOW",
            "reason":          f"Max correlation {max_corr:.0%} — acceptable"
        }

    def filter_signals(self, signals: list, returns_data: dict = None) -> list:
        """
        Filter a list of signals to remove correlated duplicates.
        When two signals are correlated, keeps the one with higher score.

        Called after scanning before placing orders.
        """
        if len(signals) <= 1:
            return signals

        if returns_data is None:
            returns_data = {}

        filtered   = []
        used_symbols = set()

        # Sort by score descending — keep best signal from each corr group
        sorted_signals = sorted(
            signals,
            key=lambda s: s.signal_score,
            reverse=True
        )

        for signal in sorted_signals:
            symbol = signal.symbol
            too_corr = False

            for kept_symbol in used_symbols:
                corr = self.calculate_correlation(
                    symbol, kept_symbol, returns_data
                )
                if corr >= MAX_CORRELATION:
                    logger.info(f"Correlation filter: skipping {symbol} "
                               f"({corr:.0%} corr with {kept_symbol})")
                    too_corr = True
                    break

            if not too_corr:
                filtered.append(signal)
                used_symbols.add(symbol)

        return filtered

    def get_portfolio_correlation_risk(self, open_positions: dict,
                                        returns_data: dict = None) -> dict:
        """
        Calculate overall portfolio correlation risk.
        High correlation = concentrated risk.
        """
        if len(open_positions) < 2:
            return {"risk_level": "LOW", "avg_correlation": 0.0}

        if returns_data is None:
            returns_data = {}

        symbols = list(open_positions.keys())
        correlations = []

        for i in range(len(symbols)):
            for j in range(i+1, len(symbols)):
                corr = self.calculate_correlation(
                    symbols[i], symbols[j], returns_data
                )
                correlations.append(corr)

        avg_corr = np.mean(correlations) if correlations else 0

        if avg_corr > 0.70:
            risk = "HIGH"
        elif avg_corr > 0.50:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        return {
            "risk_level":       risk,
            "avg_correlation":  round(avg_corr, 3),
            "n_pairs":          len(correlations),
            "symbols":          symbols,
        }


# Global instance
_filter_instance = None

def get_correlation_filter(config):
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = CorrelationFilter(config)
    return _filter_instance
