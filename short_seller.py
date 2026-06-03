"""
=============================================================================
SHORT SELLING MODULE
=============================================================================
Enables the bot to PROFIT when stocks fall by short selling.

HOW SHORT SELLING WORKS:
  1. You borrow shares from your broker
  2. Sell them immediately at current price
  3. Price falls (as you predicted)
  4. Buy them back at lower price
  5. Return shares to broker
  6. Profit = sell price - buy price

EXAMPLE:
  Short NVDA at $900 → price drops to $860 → buy back → profit $40/share

RISK:
  - Losses are theoretically unlimited (stock can keep rising)
  - This is why we use strict stops on all short positions
  - Bot only shorts when MULTIPLE bearish signals align

ALPACA SHORT SELLING:
  Alpaca supports short selling on margin accounts.
  Paper trading allows shorting freely.
  For live trading, you need a margin account.
=============================================================================
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Minimum score required to SHORT (higher bar than long trades)
MIN_SHORT_SCORE = 0.65   # More selective — shorts are riskier

# Maximum % of portfolio in short positions
MAX_SHORT_EXPOSURE = 0.30   # Never more than 30% short

# Short-specific stop loss (tighter than longs)
SHORT_STOP_MULT = 1.5    # Stop at 1.5x ATR above entry (tighter)
SHORT_TARGET_MULT = 2.5  # Target 2.5x ATR below entry


class ShortSeller:
    """
    Manages short selling logic and risk controls.
    Integrates with existing signal engine.
    """

    def __init__(self, config):
        self.config = config

    def is_shortable(self, symbol: str, signal_score: float,
                      market_regime: str = "NORMAL") -> dict:
        """
        Determine if a short trade is appropriate.
        More conservative than long trades.
        """
        # Don't short in bull markets unless very strong signal
        if market_regime == "BULL" and signal_score < 0.75:
            return {
                "shortable": False,
                "reason": "Bull market — only short with very strong signal"
            }

        # Don't short ETFs in most conditions
        etf_list = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLF",
                    "SOXX", "AIQ", "BOTZ", "ICLN"]
        if symbol in etf_list and market_regime != "BEAR":
            return {
                "shortable": False,
                "reason": "Avoid shorting ETFs unless in bear market"
            }

        # Signal must be strong enough
        if signal_score < MIN_SHORT_SCORE:
            return {
                "shortable": False,
                "reason": f"Signal {signal_score:.2f} below short threshold {MIN_SHORT_SCORE}"
            }

        return {
            "shortable": True,
            "reason": "All short criteria met"
        }

    def calculate_short_stops(self, entry_price: float,
                               atr_value: float) -> tuple:
        """
        Calculate stop loss and target for short positions.
        Stop is ABOVE entry (exits if price rises against us).
        Target is BELOW entry (profits as price falls).
        """
        stop   = entry_price + atr_value * SHORT_STOP_MULT
        target = entry_price - atr_value * SHORT_TARGET_MULT

        # Ensure valid stops
        stop   = round(max(entry_price * 1.005, stop), 4)
        target = round(max(entry_price * 0.80, target), 4)

        return stop, target

    def get_short_signals(self, df: pd.DataFrame, config) -> dict:
        """
        Generate short-specific signals.
        Looks for: breakdown patterns, distribution, weak bounces.
        """
        scores = {}

        try:
            close    = df['close'].iloc[-1]
            ema_fast = df['ema_fast'].iloc[-1]
            ema_slow = df['ema_slow'].iloc[-1]
            rsi      = df['rsi'].iloc[-1]
            bb_pct   = df['bb_pct'].iloc[-1]
            adx      = df['adx'].iloc[-1]
            minus_di = df['minus_di'].iloc[-1]
            plus_di  = df['plus_di'].iloc[-1]
            macd_hist = df['macd_hist'].iloc[-1]
            vol_ratio = df['vol_ratio'].iloc[-1]

            # Bearish EMA alignment
            if ema_fast < ema_slow and close < ema_fast:
                scores['ema_bearish'] = 0.30
            elif ema_fast < ema_slow:
                scores['ema_bearish'] = 0.15
            else:
                scores['ema_bearish'] = 0.0

            # Overbought RSI with bearish momentum
            if rsi > 70 and macd_hist < 0:
                scores['rsi_bearish'] = 0.25
            elif rsi > 65:
                scores['rsi_bearish'] = 0.15
            else:
                scores['rsi_bearish'] = 0.0

            # Price at upper Bollinger Band (overextended)
            if bb_pct > 0.95:
                scores['bb_bearish'] = 0.25
            elif bb_pct > 0.85:
                scores['bb_bearish'] = 0.10
            else:
                scores['bb_bearish'] = 0.0

            # ADX with -DI dominant (bearish trend)
            if adx > 25 and minus_di > plus_di:
                scores['adx_bearish'] = 0.20
            else:
                scores['adx_bearish'] = 0.0

            # High volume on down moves (distribution)
            recent_returns = df['close'].pct_change().iloc[-5:]
            down_vol = df['volume'].iloc[-5:][recent_returns < 0].mean()
            up_vol   = df['volume'].iloc[-5:][recent_returns > 0].mean()
            if down_vol > up_vol * 1.3:
                scores['distribution'] = 0.20
            else:
                scores['distribution'] = 0.0

            short_score = sum(scores.values())
            short_score = min(1.0, short_score)

        except Exception as e:
            logger.debug(f"Short signal error: {e}")
            short_score = 0.0
            scores = {}

        return {
            "short_score": short_score,
            "components":  scores,
            "direction":   "short" if short_score >= 0.50 else "neutral"
        }

    def check_short_exposure(self, open_positions: dict,
                              portfolio_value: float) -> dict:
        """
        Check current short exposure vs maximum allowed.
        """
        short_exposure = sum(
            pos.get('entry_exposure', 0)
            for sym, pos in open_positions.items()
            if pos.get('direction') == 'short'
        )
        short_pct = short_exposure / (portfolio_value + 1e-9)

        return {
            "short_exposure_dollar": short_exposure,
            "short_exposure_pct":    short_pct,
            "at_limit":              short_pct >= MAX_SHORT_EXPOSURE,
            "remaining_capacity":    max(0, MAX_SHORT_EXPOSURE - short_pct),
        }
