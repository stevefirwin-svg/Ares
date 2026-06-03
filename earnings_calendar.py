"""
=============================================================================
EARNINGS CALENDAR & EVENT RISK MANAGER
=============================================================================
Avoids trading around earnings announcements and major market events.

WHY THIS MATTERS:
Earnings reports cause unpredictable 5-20% moves in either direction.
No technical analysis can reliably predict earnings outcomes.
The safest approach is to avoid holding positions through earnings.

WHAT IT DOES:
  - Detects upcoming earnings using Yahoo Finance (free)
  - Flags stocks reporting within N days
  - Automatically skips or reduces size for those stocks
  - Closes existing positions before earnings if configured
  - Tracks major macro events (Fed meetings, CPI, jobs report)
=============================================================================
"""

import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Days before earnings to start avoiding the stock
EARNINGS_AVOID_DAYS_BEFORE = 3   # Avoid 3 days before
EARNINGS_AVOID_DAYS_AFTER  = 1   # Avoid 1 day after

# Major macro event dates (update monthly)
# Format: "YYYY-MM-DD": "Event Name"
MACRO_EVENTS = {
    # Add upcoming Fed meetings, CPI dates etc. here
    # Example:
    # "2026-03-19": "FOMC Meeting",
    # "2026-04-10": "CPI Report",
}


class EarningsCalendar:
    """
    Tracks earnings dates and protects positions from earnings risk.
    Uses Yahoo Finance API (free) to get earnings dates.
    """

    def __init__(self):
        self._cache = {}           # Symbol -> earnings date cache
        self._cache_date = {}      # When we last checked each symbol

    def get_next_earnings(self, symbol: str) -> Optional[datetime]:
        """
        Get the next earnings date for a symbol.
        Uses Yahoo Finance — completely free, no API key needed.
        Returns None if not found or on error.
        """
        # Check cache (valid for 24 hours)
        now = datetime.now()
        if symbol in self._cache:
            cached_time = self._cache_date.get(symbol)
            if cached_time and (now - cached_time).seconds < 86400:
                return self._cache[symbol]

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            cal    = ticker.calendar

            if cal is not None and not cal.empty:
                # Get earnings date
                if 'Earnings Date' in cal.index:
                    earnings_dates = cal.loc['Earnings Date']
                    if hasattr(earnings_dates, '__iter__'):
                        for d in earnings_dates:
                            if d and pd_to_datetime(d) > now:
                                self._cache[symbol] = pd_to_datetime(d)
                                self._cache_date[symbol] = now
                                return self._cache[symbol]

        except Exception as e:
            logger.debug(f"Could not get earnings for {symbol}: {e}")

        self._cache[symbol] = None
        self._cache_date[symbol] = now
        return None

    def pd_to_datetime(self, d) -> datetime:
        """Convert pandas Timestamp to datetime."""
        try:
            if hasattr(d, 'to_pydatetime'):
                return d.to_pydatetime().replace(tzinfo=None)
            return datetime(d.year, d.month, d.day)
        except:
            return datetime.now() + timedelta(days=365)

    def is_earnings_risk_window(self, symbol: str) -> dict:
        """
        Check if symbol is in an earnings risk window.
        Returns dict with risk level and details.
        """
        try:
            next_earnings = self.get_next_earnings(symbol)

            if next_earnings is None:
                return {
                    "in_risk_window": False,
                    "risk_level":     "NONE",
                    "days_to_earnings": None,
                    "action":          "NORMAL",
                    "reason":          "No earnings date found"
                }

            now = datetime.now()
            days_to = (next_earnings - now).days

            if days_to < 0:
                # Earnings was recent
                days_since = abs(days_to)
                if days_since <= EARNINGS_AVOID_DAYS_AFTER:
                    return {
                        "in_risk_window": True,
                        "risk_level":     "HIGH",
                        "days_to_earnings": days_to,
                        "action":          "SKIP",
                        "reason":          f"Earnings was {days_since}d ago — avoid post-earnings volatility"
                    }
                else:
                    return {
                        "in_risk_window": False,
                        "risk_level":     "NONE",
                        "days_to_earnings": days_to,
                        "action":          "NORMAL",
                        "reason":          "Past earnings, normal trading"
                    }

            elif days_to <= 1:
                return {
                    "in_risk_window": True,
                    "risk_level":     "CRITICAL",
                    "days_to_earnings": days_to,
                    "action":          "CLOSE_AND_SKIP",
                    "reason":          f"Earnings in {days_to}d — DO NOT trade"
                }

            elif days_to <= EARNINGS_AVOID_DAYS_BEFORE:
                return {
                    "in_risk_window": True,
                    "risk_level":     "HIGH",
                    "days_to_earnings": days_to,
                    "action":          "SKIP",
                    "reason":          f"Earnings in {days_to}d — skip new entries"
                }

            elif days_to <= 7:
                return {
                    "in_risk_window": True,
                    "risk_level":     "MEDIUM",
                    "days_to_earnings": days_to,
                    "action":          "REDUCE_SIZE",
                    "reason":          f"Earnings in {days_to}d — reduce position size 50%"
                }

            else:
                return {
                    "in_risk_window": False,
                    "risk_level":     "LOW",
                    "days_to_earnings": days_to,
                    "action":          "NORMAL",
                    "reason":          f"Earnings in {days_to}d — OK to trade"
                }

        except Exception as e:
            logger.debug(f"Earnings check error for {symbol}: {e}")
            return {
                "in_risk_window": False,
                "risk_level":     "UNKNOWN",
                "action":         "NORMAL",
                "reason":         "Could not check earnings"
            }

    def is_macro_risk_day(self) -> dict:
        """
        Check if today is a major macro event day.
        Fed meetings, CPI, jobs report — these move the whole market.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        if today in MACRO_EVENTS:
            return {
                "is_risk_day": True,
                "event":       MACRO_EVENTS[today],
                "action":      "REDUCE_ALL",
                "reason":      f"Major macro event today: {MACRO_EVENTS[today]}"
            }
        elif tomorrow in MACRO_EVENTS:
            return {
                "is_risk_day": True,
                "event":       MACRO_EVENTS[tomorrow],
                "action":      "REDUCE_ALL",
                "reason":      f"Major macro event tomorrow: {MACRO_EVENTS[tomorrow]}"
            }

        return {"is_risk_day": False, "action": "NORMAL"}

    def should_close_before_earnings(self, symbol: str,
                                      position: dict) -> bool:
        """
        Determine if an open position should be closed before earnings.
        Returns True if position should be closed now.
        """
        earnings_check = self.is_earnings_risk_window(symbol)
        action = earnings_check.get("action", "NORMAL")

        if action == "CLOSE_AND_SKIP":
            logger.warning(f"EARNINGS RISK: Closing {symbol} — "
                          f"{earnings_check['reason']}")
            return True
        return False

    def get_size_multiplier(self, symbol: str) -> float:
        """
        Get position size multiplier based on earnings risk.
        1.0 = normal size, 0.5 = half size, 0.0 = skip
        """
        check = self.is_earnings_risk_window(symbol)
        action = check.get("action", "NORMAL")

        if action in ("CLOSE_AND_SKIP", "SKIP"):
            return 0.0
        elif action == "REDUCE_SIZE":
            return 0.5
        else:
            return 1.0


# Global instance
earnings_calendar = EarningsCalendar()


def pd_to_datetime(d) -> datetime:
    """Convert pandas Timestamp to datetime."""
    try:
        if hasattr(d, 'to_pydatetime'):
            return d.to_pydatetime().replace(tzinfo=None)
        return datetime(d.year, d.month, d.day)
    except:
        return datetime.now() + timedelta(days=365)

# Patch into class
EarningsCalendar.pd_to_datetime = lambda self, d: pd_to_datetime(d)
