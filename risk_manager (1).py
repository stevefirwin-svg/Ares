"""
=============================================================================
RISK MANAGEMENT & POSITION SIZING ENGINE
=============================================================================
Implements multiple position sizing models:
1. Kelly Criterion (optimal growth)
2. ATR-based (volatility-adjusted)
3. Fixed fractional
4. Portfolio heat management
=============================================================================
"""

import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """Complete trade recommendation with all risk parameters."""
    symbol: str
    direction: str            # 'long' or 'short'
    entry_price: float
    stop_loss: float
    take_profit: float
    shares: int
    dollar_risk: float
    dollar_exposure: float
    position_pct: float       # % of portfolio
    win_probability: float    # Estimated win probability 0-1
    expected_value: float     # Expected value in dollars
    signal_score: float       # Composite signal score 0-1
    strategy_signals: dict    # Individual strategy contributions
    sizing_method: str        # Which sizing method was used
    estimated_cost: float = 0.0      # Total round-trip cost in dollars
    cost_pct: float = 0.0            # Cost as % of position
    net_expected_value: float = 0.0  # EV after costs
    notes: str = ""


class RiskManager:
    """
    Centralized risk management system.
    Tracks portfolio state and validates all trades.
    """

    def __init__(self, config):
        self.config = config
        self.portfolio_value = config.PORTFOLIO_SIZE
        self.daily_pnl = 0.0
        self.open_positions = {}   # symbol -> position dict
        self.trade_history = []
        self.peak_portfolio = config.PORTFOLIO_SIZE

    # ─────────────────────────────────────────────────────────────────────
    # POSITION SIZING METHODS
    # ─────────────────────────────────────────────────────────────────────

    def kelly_criterion_size(self, win_prob: float, reward_risk_ratio: float) -> float:
        """
        Kelly Criterion: f* = (bp - q) / b
        where b = reward/risk ratio, p = win prob, q = loss prob
        
        We use fractional Kelly (50%) to reduce variance.
        Returns fraction of portfolio to risk.
        """
        if win_prob <= 0 or win_prob >= 1 or reward_risk_ratio <= 0:
            return 0.0

        b = reward_risk_ratio
        p = win_prob
        q = 1 - p

        kelly_full = (b * p - q) / b
        kelly_half = kelly_full * 0.5  # Half-Kelly for safety

        # Clamp between min and max position sizes
        kelly_clamped = max(
            self.config.MIN_POSITION_SIZE_PCT,
            min(self.config.MAX_POSITION_SIZE_PCT, kelly_half)
        )

        logger.debug(f"Kelly: full={kelly_full:.3f}, half={kelly_half:.3f}, clamped={kelly_clamped:.3f}")
        return kelly_clamped

    def atr_based_size(self, entry_price: float, atr_value: float, 
                        atr_multiplier: float = 2.0) -> dict:
        """
        ATR-Based Position Sizing:
        Risk 1% of portfolio per trade, stop = entry ± 2*ATR
        Shares = (Portfolio * Risk%) / (ATR * multiplier)
        """
        dollar_risk_budget = self.portfolio_value * self.config.DEFAULT_STOP_LOSS_PCT
        stop_distance = atr_value * atr_multiplier
        
        if stop_distance <= 0:
            return {"shares": 0, "stop_distance": 0, "dollar_risk": 0}

        shares = int(dollar_risk_budget / stop_distance)
        actual_dollar_risk = shares * stop_distance
        exposure = shares * entry_price
        
        # Cap at max position size
        max_shares = int(
            (self.portfolio_value * self.config.MAX_POSITION_SIZE_PCT) / entry_price
        )
        shares = min(shares, max_shares)

        return {
            "shares": shares,
            "stop_distance": stop_distance,
            "dollar_risk": shares * stop_distance,
            "exposure": shares * entry_price,
        }

    def fixed_fractional_size(self, entry_price: float, stop_loss: float,
                               risk_fraction: float = None) -> dict:
        """
        Fixed Fractional: Risk exactly X% of portfolio on each trade.
        Shares = (Portfolio × Risk%) / |Entry - Stop|
        """
        if risk_fraction is None:
            risk_fraction = self.config.DEFAULT_STOP_LOSS_PCT

        dollar_risk = self.portfolio_value * risk_fraction
        stop_distance = abs(entry_price - stop_loss)

        if stop_distance == 0:
            return {"shares": 0, "dollar_risk": 0, "exposure": 0}

        shares = int(dollar_risk / stop_distance)
        
        # Enforce position size limits
        max_shares = int(
            (self.portfolio_value * self.config.MAX_POSITION_SIZE_PCT) / entry_price
        )
        min_shares = max(1, int(
            (self.portfolio_value * self.config.MIN_POSITION_SIZE_PCT) / entry_price
        ))
        shares = max(min_shares, min(shares, max_shares))

        return {
            "shares": shares,
            "dollar_risk": shares * stop_distance,
            "exposure": shares * entry_price,
        }

    def optimal_position_size(self, entry_price: float, stop_loss: float,
                               take_profit: float, win_prob: float,
                               atr_value: float, signal_score: float) -> dict:
        """
        Master sizing function: blends Kelly + ATR + Fixed Fractional.
        Uses signal strength to scale position size.
        
        Higher signal score → closer to Kelly optimal
        Lower signal score  → falls back to conservative fixed fractional
        """
        reward_risk = abs(take_profit - entry_price) / abs(entry_price - stop_loss + 1e-9)

        # Get each method's recommendation
        kelly_pct = self.kelly_criterion_size(win_prob, reward_risk)
        atr_result = self.atr_based_size(entry_price, atr_value)
        ff_result  = self.fixed_fractional_size(entry_price, stop_loss)

        # Blend based on signal confidence
        # High signal (>0.75): favor Kelly. Medium: blend. Low: fixed fractional.
        if signal_score >= 0.75:
            weights = {"kelly": 0.5, "atr": 0.3, "ff": 0.2}
        elif signal_score >= 0.60:
            weights = {"kelly": 0.25, "atr": 0.40, "ff": 0.35}
        else:
            weights = {"kelly": 0.1, "atr": 0.3, "ff": 0.6}

        atr_pct = atr_result["exposure"] / self.portfolio_value if self.portfolio_value > 0 else 0
        ff_pct  = ff_result["exposure"] / self.portfolio_value if self.portfolio_value > 0 else 0

        blended_pct = (
            weights["kelly"] * kelly_pct +
            weights["atr"]   * atr_pct   +
            weights["ff"]    * ff_pct
        )
        blended_pct = max(
            self.config.MIN_POSITION_SIZE_PCT,
            min(self.config.MAX_POSITION_SIZE_PCT, blended_pct)
        )

        # Apply portfolio heat penalty
        blended_pct *= self._portfolio_heat_multiplier()

        # Convert to shares
        dollar_exposure = self.portfolio_value * blended_pct
        shares = max(1, int(dollar_exposure / entry_price))
        actual_exposure = shares * entry_price
        actual_risk = shares * abs(entry_price - stop_loss)

        method = (
            "Kelly-dominant" if weights["kelly"] >= 0.4
            else "Blended" if weights["atr"] >= 0.4
            else "Fixed-Fractional"
        )

        return {
            "shares": shares,
            "dollar_exposure": actual_exposure,
            "dollar_risk": actual_risk,
            "position_pct": actual_exposure / self.portfolio_value,
            "reward_risk": reward_risk,
            "method": method,
        }

    def _portfolio_heat_multiplier(self) -> float:
        """
        Portfolio heat: reduce position size when already heavily exposed.
        Full size at 0 positions, scaled down as we approach max positions.
        """
        n_positions = len(self.open_positions)
        max_pos = self.config.MAX_OPEN_POSITIONS

        if n_positions == 0:
            return 1.0
        elif n_positions >= max_pos - 1:
            return 0.5
        else:
            return 1.0 - (n_positions / max_pos) * 0.4

    # ─────────────────────────────────────────────────────────────────────
    # TRADING COST MODEL
    # ─────────────────────────────────────────────────────────────────────

    def estimate_round_trip_cost(self, entry_price: float, shares: int,
                                  symbol: str = "") -> dict:
        """
        Estimate realistic total round-trip trading costs (entry + exit).

        Components:
        - Commission:   $0 on Alpaca (but we add a tiny buffer for safety)
        - Bid/ask spread: 0.03% per side = 0.06% round trip (conservative for liquid stocks)
        - Slippage:     0.05% per side = 0.10% round trip (market impact + timing)
        - SEC sell fee: ~$0.000008 per dollar sold (tiny but included)

        Philosophy: We use FAIR estimates, not worst-case. These numbers reflect
        real conditions on liquid large-cap stocks. We do NOT double-penalize --
        the spread is already partially accounted for in limit order pricing.

        Returns dict with cost breakdown and total cost in dollars.
        """
        dollar_value = entry_price * shares

        # Commission — Alpaca is $0, but add $0.01 per side as buffer
        commission_per_side = 0.01
        commission_total = commission_per_side * 2  # entry + exit

        # Bid/ask spread — 0.03% per side on liquid stocks (SPY/AAPL etc ~0.01%, less liquid ~0.05%)
        # Using 0.03% as a fair middle ground
        spread_per_side = dollar_value * 0.0003
        spread_total = spread_per_side * 2

        # Slippage — 0.04% per side (realistic for 5-min bars on liquid stocks)
        slippage_per_side = dollar_value * 0.0004
        slippage_total = slippage_per_side * 2

        # SEC fee — only on sell side, $8 per $1,000,000 sold
        sec_fee = dollar_value * 0.000008

        total_cost = commission_total + spread_total + slippage_total + sec_fee

        # Cost as % of position
        cost_pct = total_cost / dollar_value if dollar_value > 0 else 0

        return {
            "commission": round(commission_total, 4),
            "spread": round(spread_total, 4),
            "slippage": round(slippage_total, 4),
            "sec_fee": round(sec_fee, 6),
            "total_cost": round(total_cost, 4),
            "cost_pct": round(cost_pct * 100, 4),  # as percentage
            "dollar_value": round(dollar_value, 2),
        }

    def adjust_expected_value_for_costs(self, raw_ev: float, cost_estimate: dict,
                                         win_prob: float) -> tuple[float, float]:
        """
        Subtract trading costs from raw expected value.

        We apply costs to BOTH winning and losing trades since you pay
        spread/slippage regardless of outcome.

        Returns: (cost_adjusted_ev, breakeven_win_rate)
        """
        total_cost = cost_estimate["total_cost"]

        # EV after costs — subtract full round-trip cost
        adjusted_ev = raw_ev - total_cost

        # Breakeven win rate: what win rate do you need just to cover costs?
        # At breakeven: P(win)*avg_win = P(loss)*avg_loss + costs
        # This is informational — we don't hard-filter on it
        breakeven_wr = 0.5  # default
        if raw_ev > 0 and win_prob > 0:
            # Rough estimate of avg win/loss from EV and win prob
            # Not perfect but gives a useful sanity check number
            implied_reward = raw_ev / (win_prob + 1e-9)
            implied_risk = implied_reward * 0.5  # assumes ~2:1 RR
            if implied_reward + implied_risk > 0:
                breakeven_wr = (implied_risk + total_cost) / (implied_reward + implied_risk)

        return round(adjusted_ev, 4), round(min(0.75, max(0.40, breakeven_wr)), 4)

    # ─────────────────────────────────────────────────────────────────────
    # STOP LOSS & TAKE PROFIT CALCULATION
    # ─────────────────────────────────────────────────────────────────────

    def calculate_stops(self, entry_price: float, direction: str,
                         atr_value: float, support: float = None,
                         resistance: float = None) -> tuple:
        """
        Smart stop loss and take profit using ATR + key levels.
        
        For longs:  stop = entry - 2*ATR (or below support)
                    target = entry + 3*ATR (or below resistance)
        For shorts: inverse
        """
        atr_stop_dist = atr_value * 2.0
        atr_target_dist = atr_value * 3.0

        if direction == 'long':
            stop = entry_price - atr_stop_dist
            target = entry_price + atr_target_dist

            # Respect key levels if provided
            if support is not None and support < entry_price:
                stop = max(stop, support - atr_value * 0.5)
            if resistance is not None and resistance > entry_price:
                target = min(target, resistance - atr_value * 0.1)

        else:  # short
            stop = entry_price + atr_stop_dist
            target = entry_price - atr_target_dist

            if resistance is not None and resistance > entry_price:
                stop = min(stop, resistance + atr_value * 0.5)
            if support is not None and support < entry_price:
                target = max(target, support + atr_value * 0.1)

        # Enforce minimum reward/risk of 1.5:1
        risk = abs(entry_price - stop)
        reward = abs(target - entry_price)
        if reward / (risk + 1e-9) < 1.5:
            if direction == 'long':
                target = entry_price + risk * 2.0
            else:
                target = entry_price - risk * 2.0

        return round(stop, 4), round(target, 4)

    # ─────────────────────────────────────────────────────────────────────
    # TRADE VALIDATION
    # ─────────────────────────────────────────────────────────────────────

    def validate_trade(self, signal: TradeSignal) -> tuple[bool, str]:
        """
        Gate-check every proposed trade. Returns (approved, reason).
        ALL checks must pass before an order is placed.
        """
        # 1. Daily loss limit
        daily_loss_pct = -self.daily_pnl / self.portfolio_value
        if daily_loss_pct >= self.config.MAX_DAILY_LOSS_PCT:
            return False, f"Daily loss limit reached ({daily_loss_pct:.1%})"

        # 2. Max drawdown
        drawdown = (self.peak_portfolio - self.portfolio_value) / self.peak_portfolio
        if drawdown >= self.config.MAX_DRAWDOWN_PCT:
            return False, f"Max drawdown limit reached ({drawdown:.1%})"

        # 3. Max open positions
        if len(self.open_positions) >= self.config.MAX_OPEN_POSITIONS:
            return False, f"Max positions reached ({len(self.open_positions)})"

        # 4. Already in this symbol
        if signal.symbol in self.open_positions:
            return False, f"Already in position for {signal.symbol}"

        # 5. Minimum signal score
        if signal.signal_score < self.config.MIN_SIGNAL_SCORE:
            return False, f"Signal score too low ({signal.signal_score:.2f} < {self.config.MIN_SIGNAL_SCORE})"

        # 6. Minimum win probability
        if signal.win_probability < self.config.MIN_WIN_PROBABILITY:
            return False, f"Win probability too low ({signal.win_probability:.1%})"

        # 7. Positive expected value
        if signal.expected_value <= 0:
            return False, f"Negative expected value (${signal.expected_value:.2f})"

        # 8. Sufficient capital
        if signal.dollar_exposure > self.portfolio_value * 0.95:
            return False, "Insufficient capital"

        # 9. Position size limits
        if signal.position_pct > self.config.MAX_POSITION_SIZE_PCT:
            return False, f"Position too large ({signal.position_pct:.1%})"

        # 10. Reward/Risk minimum
        if signal.take_profit and signal.stop_loss and signal.entry_price:
            reward = abs(signal.take_profit - signal.entry_price)
            risk = abs(signal.entry_price - signal.stop_loss)
            if risk > 0 and reward / risk < 1.5:
                return False, f"R/R ratio too low ({reward/risk:.2f})"

        # 11. Cost-adjusted expected value check (LENIENT)
        # Only reject if costs completely wipe out the edge AND the trade is tiny
        # We want to catch fee-eating trades without blocking good ones
        costs = self.estimate_round_trip_cost(
            signal.entry_price, signal.shares
        )
        cost_adjusted_ev, breakeven_wr = self.adjust_expected_value_for_costs(
            signal.expected_value, costs, signal.win_probability
        )

        # Only block if: cost > 40% of gross expected value AND position is very small
        # This allows normal trades through while blocking truly fee-dominated micro-trades
        if signal.expected_value > 0:
            cost_ratio = costs["total_cost"] / (signal.expected_value + 1e-9)
            if cost_ratio > 0.40 and signal.dollar_exposure < 150:
                return False, (
                    f"Costs (${costs['total_cost']:.2f}) eat >{cost_ratio:.0%} "
                    f"of edge on small position (${signal.dollar_exposure:.0f})"
                )

        # Log cost info for transparency (never blocks a good trade silently)
        logger.debug(
            f"Cost check {signal.symbol}: "
            f"gross EV=${signal.expected_value:.2f} | "
            f"costs=${costs['total_cost']:.2f} ({costs['cost_pct']:.3f}%) | "
            f"net EV=${cost_adjusted_ev:.2f} | "
            f"breakeven win rate={breakeven_wr:.1%}"
        )

        return True, "Approved"

    def update_portfolio(self, trade_result: dict):
        """Update portfolio state after a trade closes."""
        pnl = trade_result.get('pnl', 0)
        self.daily_pnl += pnl
        self.portfolio_value += pnl
        self.peak_portfolio = max(self.peak_portfolio, self.portfolio_value)
        self.trade_history.append(trade_result)
        
        symbol = trade_result.get('symbol')
        if symbol in self.open_positions:
            del self.open_positions[symbol]

        logger.info(f"Trade closed: {symbol} PnL=${pnl:.2f} | Portfolio=${self.portfolio_value:.2f}")

    def add_position(self, signal: TradeSignal):
        """Record a new open position."""
        self.open_positions[signal.symbol] = {
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "shares": signal.shares,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "entry_exposure": signal.dollar_exposure,
            "peak_price": signal.entry_price,
        }

    def update_trailing_stop(self, symbol: str, current_price: float) -> Optional[float]:
        """Update trailing stop if price moves favorably."""
        if symbol not in self.open_positions:
            return None
        
        pos = self.open_positions[symbol]
        trail_pct = self.config.TRAILING_STOP_PCT

        if pos['direction'] == 'long':
            pos['peak_price'] = max(pos['peak_price'], current_price)
            new_stop = pos['peak_price'] * (1 - trail_pct)
            if new_stop > pos['stop_loss']:
                pos['stop_loss'] = new_stop
                return new_stop
        else:
            pos['peak_price'] = min(pos['peak_price'], current_price)
            new_stop = pos['peak_price'] * (1 + trail_pct)
            if new_stop < pos['stop_loss']:
                pos['stop_loss'] = new_stop
                return new_stop

        return None

    def should_exit(self, symbol: str, current_price: float) -> tuple[bool, str]:
        """Check if an open position should be exited."""
        if symbol not in self.open_positions:
            return False, ""

        pos = self.open_positions[symbol]

        if pos['direction'] == 'long':
            if current_price <= pos['stop_loss']:
                return True, "stop_loss"
            if current_price >= pos['take_profit']:
                return True, "take_profit"
        else:
            if current_price >= pos['stop_loss']:
                return True, "stop_loss"
            if current_price <= pos['take_profit']:
                return True, "take_profit"

        return False, ""

    # ─────────────────────────────────────────────────────────────────────
    # PORTFOLIO STATISTICS
    # ─────────────────────────────────────────────────────────────────────

    def get_statistics(self) -> dict:
        """Compute performance statistics from trade history."""
        if not self.trade_history:
            return {}

        pnls = [t.get('pnl', 0) for t in self.trade_history]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / len(pnls) if pnls else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses else float('inf')

        # Sharpe Ratio (simplified, assumes daily data)
        if len(pnls) > 1:
            returns = np.array(pnls) / self.config.PORTFOLIO_SIZE
            sharpe = np.mean(returns) / (np.std(returns) + 1e-9) * np.sqrt(252)
        else:
            sharpe = 0

        # Max Drawdown
        cumulative = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = running_max - cumulative
        max_dd = np.max(drawdowns) if len(drawdowns) > 0 else 0

        return {
            "total_trades": len(pnls),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_dd,
            "total_pnl": sum(pnls),
            "portfolio_value": self.portfolio_value,
            "return_pct": (self.portfolio_value - self.config.PORTFOLIO_SIZE) / self.config.PORTFOLIO_SIZE,
        }
