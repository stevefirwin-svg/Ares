"""
=============================================================================
DYNAMIC CONVICTION-BASED POSITION SIZING
=============================================================================
Scales position size based on signal strength, market conditions,
and historical accuracy. High conviction = larger position.
Low conviction = smaller position or no trade.

CONVICTION LEVELS:
  ELITE    (score 15-17): 100% of max position size
  STRONG   (score 12-14): 75% of max position size
  GOOD     (score 9-11):  50% of max position size
  MARGINAL (score 6-8):   25% of max position size
  SKIP     (score 0-5):   No trade
=============================================================================
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)


class ConvictionSizer:
    """
    Dynamically sizes positions based on signal conviction.
    Integrates with GARCH volatility, Kelly Criterion,
    and historical win rates per symbol.
    """

    def __init__(self, config):
        self.config = config
        self.symbol_history = {}   # Tracks per-symbol win rates
        self.market_regime  = "NORMAL"

    def get_conviction_multiplier(self, score_17: float, grade: str) -> float:
        """
        Convert 17-point score to position size multiplier.
        Elite setups get full size. Marginal setups get quarter size.
        """
        if score_17 >= 15:
            return 1.00   # ELITE — full position
        elif score_17 >= 12:
            return 0.75   # STRONG — 75%
        elif score_17 >= 9:
            return 0.50   # GOOD — 50%
        elif score_17 >= 6:
            return 0.25   # MARGINAL — 25%
        else:
            return 0.00   # SKIP

    def get_volatility_multiplier(self, garch_regime: str,
                                   hist_vol: float) -> float:
        """
        Reduce position size in high volatility environments.
        Balanced approach — never goes to zero, just reduces.
        """
        if garch_regime == "LOW" or hist_vol < 15:
            return 1.10   # Low vol — slight boost
        elif garch_regime == "NORMAL" or hist_vol < 25:
            return 1.00   # Normal — no change
        elif garch_regime == "ELEVATED" or hist_vol < 35:
            return 0.75   # Elevated — reduce 25%
        else:
            return 0.50   # High vol — cut in half

    def get_symbol_accuracy_multiplier(self, symbol: str) -> float:
        """
        Boost size for symbols the bot has historically done well on.
        Reduce size for symbols with poor track record.
        Starts neutral until 10+ trades recorded.
        """
        if symbol not in self.symbol_history:
            return 1.00   # No history — neutral

        history = self.symbol_history[symbol]
        if len(history) < 10:
            return 1.00   # Not enough data yet

        win_rate = sum(1 for t in history if t['won']) / len(history)

        if win_rate >= 0.65:
            return 1.20   # Strong track record — boost 20%
        elif win_rate >= 0.55:
            return 1.10   # Good track record — boost 10%
        elif win_rate >= 0.45:
            return 1.00   # Average — neutral
        elif win_rate >= 0.35:
            return 0.75   # Below average — reduce
        else:
            return 0.50   # Poor track record — cut in half

    def get_portfolio_heat_multiplier(self, open_positions: dict,
                                       portfolio_value: float) -> float:
        """
        Reduce new position sizes as portfolio gets more exposed.
        Prevents overtrading and protects against correlated losses.
        """
        if not open_positions:
            return 1.00

        # Calculate current portfolio heat (% exposed)
        total_exposure = sum(
            pos.get('entry_exposure', 0)
            for pos in open_positions.values()
        )
        heat_pct = total_exposure / (portfolio_value + 1e-9)

        if heat_pct < 0.30:
            return 1.00   # Under 30% exposed — full size
        elif heat_pct < 0.50:
            return 0.85   # 30-50% exposed — slight reduction
        elif heat_pct < 0.70:
            return 0.70   # 50-70% exposed — meaningful reduction
        else:
            return 0.50   # Over 70% exposed — half size

    def get_market_regime_multiplier(self) -> float:
        """
        Adjust sizing based on broad market regime.
        In bear markets, reduce all position sizes.
        """
        regime_map = {
            "BULL":     1.10,
            "NORMAL":   1.00,
            "CHOPPY":   0.80,
            "BEAR":     0.60,
            "CRISIS":   0.40,
        }
        return regime_map.get(self.market_regime, 1.00)

    def calculate_conviction_size(self, symbol: str, score_17: float,
                                   grade: str, garch_regime: str,
                                   hist_vol: float, open_positions: dict,
                                   portfolio_value: float,
                                   base_shares: int,
                                   entry_price: float) -> dict:
        """
        Master conviction sizing function.
        Combines all multipliers into final share count.

        Returns dict with shares, multipliers, and reasoning.
        """
        # Get all multipliers
        conviction_mult   = self.get_conviction_multiplier(score_17, grade)
        volatility_mult   = self.get_volatility_multiplier(garch_regime, hist_vol)
        accuracy_mult     = self.get_symbol_accuracy_multiplier(symbol)
        heat_mult         = self.get_portfolio_heat_multiplier(
                                open_positions, portfolio_value)
        regime_mult       = self.get_market_regime_multiplier()

        # Skip low conviction trades entirely
        if conviction_mult == 0:
            return {
                "shares": 0,
                "skip": True,
                "reason": f"Score {score_17:.1f}/17 below minimum threshold",
                "conviction_mult": 0,
            }

        # Combined multiplier (balanced — never below 15% or above 120%)
        combined = conviction_mult * volatility_mult * accuracy_mult * \
                   heat_mult * regime_mult
        combined = max(0.15, min(1.20, combined))

        # Apply to base shares
        final_shares = max(1, int(base_shares * combined))

        # Cap at max position size
        max_shares = max(1, int(
            (portfolio_value * self.config.MAX_POSITION_SIZE_PCT) / (entry_price + 1e-9)
        ))
        final_shares = min(final_shares, max_shares)

        dollar_exposure = final_shares * entry_price
        position_pct    = dollar_exposure / portfolio_value

        logger.info(f"Conviction sizing {symbol}: "
                   f"base={base_shares} | "
                   f"conviction={conviction_mult:.2f} | "
                   f"vol={volatility_mult:.2f} | "
                   f"accuracy={accuracy_mult:.2f} | "
                   f"heat={heat_mult:.2f} | "
                   f"combined={combined:.2f} | "
                   f"final={final_shares}")

        return {
            "shares":           final_shares,
            "skip":             False,
            "combined_mult":    round(combined, 3),
            "conviction_mult":  conviction_mult,
            "volatility_mult":  volatility_mult,
            "accuracy_mult":    accuracy_mult,
            "heat_mult":        heat_mult,
            "regime_mult":      regime_mult,
            "dollar_exposure":  round(dollar_exposure, 2),
            "position_pct":     round(position_pct, 4),
            "reasoning":        (
                f"Score {score_17:.1f}/17 [{grade}] → "
                f"conviction {conviction_mult:.0%} × "
                f"vol {volatility_mult:.0%} × "
                f"accuracy {accuracy_mult:.0%} = "
                f"{combined:.0%} of base size"
            )
        }

    def record_trade_result(self, symbol: str, won: bool, pnl: float):
        """Record trade outcome to improve future sizing for this symbol."""
        if symbol not in self.symbol_history:
            self.symbol_history[symbol] = []
        self.symbol_history[symbol].append({"won": won, "pnl": pnl})
        # Keep last 50 trades per symbol
        self.symbol_history[symbol] = self.symbol_history[symbol][-50:]

    def update_market_regime(self, spy_return_20d: float,
                              vix_level: float = None):
        """Update broad market regime based on SPY performance."""
        if spy_return_20d > 0.05:
            self.market_regime = "BULL"
        elif spy_return_20d > 0.00:
            self.market_regime = "NORMAL"
        elif spy_return_20d > -0.05:
            self.market_regime = "CHOPPY"
        elif spy_return_20d > -0.15:
            self.market_regime = "BEAR"
        else:
            self.market_regime = "CRISIS"

        logger.info(f"Market regime: {self.market_regime} "
                   f"(SPY 20d: {spy_return_20d:+.1%})")
