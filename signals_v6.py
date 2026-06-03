"""
Raptor v6.0 — Advanced Technical Analysis Signal Engine
========================================================

20 factors across 6 clusters. Pure TA. No sentiment. Every factor
has peer-reviewed evidence for the 1-day to 6-week swing horizon.

DESIGN PRINCIPLES:
  1. Every factor measures something DIFFERENT (orthogonalization enforces this)
  2. Every factor has been published in academic research with documented edge
  3. No factor duplicates another's information (RSI and Stochastic are NOT both included)
  4. Factors are organized by WHAT QUESTION they answer:

  CLUSTER 1: MEAN-REVERSION — "Is this stock stretched and likely to snap back?"
  CLUSTER 2: MOMENTUM & TREND — "Is the larger trend intact to support the entry?"
  CLUSTER 3: SUPPORT/RESISTANCE — "Is price at a level where orders cluster?"
  CLUSTER 4: VOLUME & FLOW — "Is institutional money confirming the setup?"
  CLUSTER 5: VOLATILITY — "What is the vol regime and is a squeeze coming?"
  CLUSTER 6: RELATIVE VALUE — "Is this stock outperforming/underperforming its sector?"

FACTOR LIBRARY (20 factors):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  MEAN-REVERSION (30%):
    rsi_mr            RSI(5) continuous MR signal           Wilder 1978, Poterba & Summers 1988
    bollinger_z       Z-score within Bollinger Bands        Bollinger 2002
    williams_r        Williams %R (High-Close range)        Williams 1979 — NOT redundant with RSI
                      because it uses High-to-Close, not avg gain/loss
    ma_deviation      Distance from 8/21/50 EMA average     Lo & MacKinlay 1990
    hurst             Hurst exponent (R/S analysis)         Mandelbrot & Wallis 1969

  MOMENTUM & TREND (22%):
    ma_stack           8/21/50 EMA ordering + slope         Jegadeesh & Titman 1993
    macd_accel         MACD histogram acceleration          Appel 1979
    adx_signed         ADX * sign(+DI - -DI)               Wilder 1978
    roc_cascade        Multi-period ROC alignment           Jegadeesh & Titman 1993
                       (5/10/20 day slopes aligned = strong)

  SUPPORT/RESISTANCE (15%):
    fib_proximity      Distance to nearest Fibonacci level  Osler 2000, 2003
                       (38.2%, 50%, 61.8% of last swing)   Orders cluster at fibs = self-fulfilling SR
    pivot_proximity    Distance to classic pivot points     Person 2004
                       (floor trader method: PP, S1, R1)   Institutional benchmark levels
    keltner_position   Position within Keltner Channel      Keltner 1960, ATR-based = different info
                       (ATR bands vs Bollinger's std dev)   than Bollinger which uses standard deviation
    ichimoku_signal    Ichimoku cloud composite signal      Hosoda 1968, validated Pongsapan 2016
                       (Tenkan/Kijun cross + cloud pos)    5-in-1 system: trend, momentum, SR, cloud

  VOLUME & FLOW (15%):
    obv_r2             OBV regression slope * R-squared     Granville 1963
    vpt_slope          Volume-Price Trend regression        Markstein 1966 — weights by % price change
                       (different from OBV: proportional)   not just direction. Captures conviction.
    accum_dist         Chaikin Accumulation/Distribution    Chaikin 1970s, close-location-value
    mfi_signal         Money Flow Index (vol-weighted RSI)  Quong & Soudack 1989

  VOLATILITY (10%):
    atr_pctile         ATR percentile of 60-day dist       Wilder 1978
    bb_squeeze         Bollinger bandwidth percentile       Bollinger 2002, squeeze = breakout precursor

  RELATIVE VALUE (8%):
    rel_strength       10-day return vs SPY                 Levy 1967, relative strength rotation

Bar requirements:
  Maximum: 80 bars (bb_squeeze: 60 pctile window + 20 Bollinger warmup)
  Config lookback_days=150 calendar ~ 105 trading bars. All covered.

Pipeline: raw -> cross-sectional z-score -> within-cluster orthogonalize
          -> regime-conditional weight -> composite -> t-test -> Kelly size
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from config import RaptorConfig

logger = logging.getLogger("raptor.v6.signals")

MIN_BARS_REQUIRED = 80


@dataclass
class Signal:
    symbol: str
    side: str
    composite_score: float
    composite_percentile: float
    t_statistic: float
    factor_scores: Dict[str, float]
    factor_contributions: Dict[str, float]
    factors_positive: int
    regime: str
    sentiment_score: float
    atr: float
    entry_price: float
    stop_price: float
    take_profit: float
    kelly_fraction: float
    hold_target_days: int
    timestamp: str


# ═══════════════════════════════════════════════════════════════════════════════
# FACTOR LIBRARY — 20 FACTORS
# ═══════════════════════════════════════════════════════════════════════════════

class FactorsV6:
    """
    Pure TA factors. Each returns a raw float or np.nan.
    No normalization — that happens cross-sectionally in the engine.
    """

    # ── MEAN-REVERSION (5 factors) ───────────────────────────────────────────

    @staticmethod
    def rsi_mr(c: pd.Series, period: int = 5) -> float:
        """
        RSI(5) mapped to mean-reversion signal. Wilder 1978.
        Continuous [-1, +1]. Oversold = positive (buy signal).
        Short-period RSI captures rapid momentum exhaustion.
        """
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
        rsi = 100.0 - 100.0 / (1.0 + gain / (loss + 1e-10))
        return float((50.0 - rsi.iloc[-1]) / 50.0)

    @staticmethod
    def bollinger_z(c: pd.Series, period: int = 20) -> float:
        """
        Z-score within Bollinger Bands. Bollinger 2002.
        Negative z = below midline = oversold for MR.
        This is the statistical deviation from the rolling mean.
        """
        sma = c.rolling(period).mean().iloc[-1]
        std = c.rolling(period).std().iloc[-1]
        if std < 1e-10:
            return 0.0
        return float(-(c.iloc[-1] - sma) / std)

    @staticmethod
    def williams_r(df: pd.DataFrame, period: int = 14) -> float:
        """
        Williams %R. Larry Williams 1979.
        Uses (Highest High - Close) / (Highest High - Lowest Low).
        NOT redundant with RSI: RSI uses avg gain/loss ratio,
        Williams uses price position in the high-low range.
        Captures different information about exhaustion.
        Returns [-1, +1]: oversold = positive.
        """
        hi = df["high"].rolling(period).max()
        lo = df["low"].rolling(period).min()
        wr = -100.0 * (hi.iloc[-1] - df["close"].iloc[-1]) / (hi.iloc[-1] - lo.iloc[-1] + 1e-10)
        # %R ranges from -100 (oversold) to 0 (overbought)
        # Map to [-1, +1]: -100 -> +1 (oversold=buy), 0 -> -1 (overbought=sell)
        return float((-wr - 50.0) / 50.0)

    @staticmethod
    def ma_deviation(c: pd.Series) -> float:
        """
        Distance from 8/21/50 EMA average. Lo & MacKinlay 1990.
        Multi-timeframe equilibrium. Below = oversold for MR.
        """
        e8 = c.ewm(span=8, adjust=False).mean().iloc[-1]
        e21 = c.ewm(span=21, adjust=False).mean().iloc[-1]
        e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
        avg = (e8 + e21 + e50) / 3.0
        if avg == 0:
            return 0.0
        return float(-(c.iloc[-1] - avg) / avg)

    @staticmethod
    def hurst(c: pd.Series, max_lag: int = 20) -> float:
        """
        Hurst exponent via R/S analysis. Mandelbrot & Wallis 1969.
        H < 0.5 = mean-reverting (anti-persistent).
        H > 0.5 = trending (persistent).
        Returns (0.5 - H): positive when mean-reverting.
        This is the mathematical PROOF that MR exists in this series.
        """
        rets = np.log(c / c.shift(1)).dropna().values
        if len(rets) < max_lag * 2:
            return np.nan
        rs_pts = []
        for lag in range(2, max_lag + 1):
            n_sub = len(rets) // lag
            if n_sub < 1:
                continue
            rs_list = []
            for i in range(n_sub):
                sub = rets[i * lag:(i + 1) * lag]
                dev = np.cumsum(sub - sub.mean())
                R = dev.max() - dev.min()
                S = sub.std()
                if S > 1e-10:
                    rs_list.append(R / S)
            if rs_list:
                rs_pts.append((np.log(lag), np.log(np.mean(rs_list))))
        if len(rs_pts) < 4:
            return np.nan
        x = np.array([p[0] for p in rs_pts])
        y = np.array([p[1] for p in rs_pts])
        H = np.polyfit(x, y, 1)[0]
        return float(0.5 - H)

    # ── MOMENTUM & TREND (4 factors) ─────────────────────────────────────────

    @staticmethod
    def ma_stack(c: pd.Series) -> float:
        """
        EMA stack alignment + slope. Jegadeesh & Titman 1993.
        Bull stack (8 > 21 > 50, all rising) = +1.
        Bear stack = -1. Confirms trend health for MR entries.
        """
        e8 = c.ewm(span=8, adjust=False).mean()
        e21 = c.ewm(span=21, adjust=False).mean()
        e50 = c.ewm(span=50, adjust=False).mean()
        order = float((e8.iloc[-1] > e21.iloc[-1]) + (e21.iloc[-1] > e50.iloc[-1]) - 1)
        s8 = (e8.iloc[-1] / e8.iloc[-5] - 1) if len(e8) >= 5 else 0
        s21 = (e21.iloc[-1] / e21.iloc[-5] - 1) if len(e21) >= 5 else 0
        s50 = (e50.iloc[-1] / e50.iloc[-5] - 1) if len(e50) >= 5 else 0
        slope = np.clip((s8 + s21 + s50) / 3.0 * 50.0, -0.4, 0.4)
        return float(order * 0.6 + slope)

    @staticmethod
    def macd_accel(c: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> float:
        """
        MACD histogram 5-bar regression slope / price. Appel 1979.
        Measures momentum ACCELERATION. Positive slope = improving
        even if MACD histogram is still negative (early reversal).
        """
        ef = c.ewm(span=fast, adjust=False).mean()
        es = c.ewm(span=slow, adjust=False).mean()
        hist = ef - es - (ef - es).ewm(span=sig, adjust=False).mean()
        y = hist.iloc[-5:].values
        if len(y) < 5:
            return np.nan
        return float(np.polyfit(np.arange(5), y, 1)[0] / c.iloc[-1])

    @staticmethod
    def adx_signed(df: pd.DataFrame, period: int = 14) -> float:
        """
        Signed ADX: strength * direction. Wilder 1978.
        Positive = strong uptrend. Negative = strong downtrend.
        Low absolute value = no trend (range-bound).
        """
        h, l, c = df["high"], df["low"], df["close"]
        pdm = h.diff().clip(lower=0)
        mdm = (-l.diff()).clip(lower=0)
        pdm[pdm < mdm] = 0.0
        mdm[mdm < pdm] = 0.0
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.ewm(span=period, adjust=False).mean()
        pdi = 100.0 * pdm.ewm(span=period, adjust=False).mean() / atr_s
        mdi = 100.0 * mdm.ewm(span=period, adjust=False).mean() / atr_s
        dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
        adx = dx.ewm(span=period, adjust=False).mean()
        sign = 1.0 if pdi.iloc[-1] > mdi.iloc[-1] else -1.0
        return float(adx.iloc[-1] * sign)

    @staticmethod
    def roc_cascade(c: pd.Series) -> float:
        """
        Multi-period ROC alignment. Jegadeesh & Titman 1993.
        Averages the sign-aligned slope of 5-day, 10-day, and 20-day ROC.
        When all three timeframes agree on direction, the signal is strong.
        When they diverge, the signal weakens. Captures intermediate
        momentum — the strongest documented anomaly in finance.
        """
        if len(c) < 21:
            return np.nan
        roc5 = (c.iloc[-1] / c.iloc[-6]) - 1.0
        roc10 = (c.iloc[-1] / c.iloc[-11]) - 1.0
        roc20 = (c.iloc[-1] / c.iloc[-21]) - 1.0

        # Alignment score: if all same sign, magnitude amplified
        signs = np.sign([roc5, roc10, roc20])
        alignment = abs(signs.sum()) / 3.0  # 1.0 = all aligned, 0.33 = mixed

        avg_roc = (roc5 + roc10 + roc20) / 3.0
        return float(avg_roc * (0.5 + 0.5 * alignment))

    # ── SUPPORT/RESISTANCE (4 factors) ───────────────────────────────────────

    @staticmethod
    def fib_proximity(df: pd.DataFrame, lookback: int = 50) -> float:
        """
        Proximity to Fibonacci retracement levels. Osler 2000, 2003.
        Finds the most recent significant swing high/low, computes
        38.2%, 50.0%, 61.8% retracement levels, and measures how
        close price is to the nearest level.

        Academic evidence: Osler (2003) proved statistically significant
        order clustering at Fibonacci levels in FX and equity markets,
        creating self-fulfilling support/resistance zones.

        Returns positive when price is AT a fib level (potential bounce).
        """
        window = df.tail(lookback)
        if len(window) < 20:
            return np.nan

        swing_high = window["high"].max()
        swing_low = window["low"].min()
        swing_range = swing_high - swing_low
        if swing_range < 1e-10:
            return 0.0

        price = df["close"].iloc[-1]

        # Fibonacci levels (retracement from high)
        fib_levels = [
            swing_high - 0.236 * swing_range,
            swing_high - 0.382 * swing_range,
            swing_high - 0.500 * swing_range,
            swing_high - 0.618 * swing_range,
            swing_high - 0.786 * swing_range,
        ]

        # Distance to nearest fib level as % of swing range
        distances = [abs(price - level) / swing_range for level in fib_levels]
        min_distance = min(distances)

        # Close to fib = positive signal (0 distance = 1.0, far = 0.0)
        # Invert: smaller distance = stronger signal
        proximity = max(0.0, 1.0 - min_distance * 5.0)

        # Sign: if price is above nearest fib = support below (bullish)
        nearest_idx = distances.index(min_distance)
        nearest_level = fib_levels[nearest_idx]
        if price >= nearest_level:
            return float(proximity)
        else:
            return float(-proximity * 0.5)  # Below fib = weaker, could break

    @staticmethod
    def pivot_proximity(df: pd.DataFrame) -> float:
        """
        Classic floor trader pivot points. Person 2004.
        PP = (H + L + C) / 3
        S1 = 2*PP - H,  R1 = 2*PP - L
        S2 = PP - (H-L), R2 = PP + (H-L)

        Measures proximity to nearest pivot level. Institutional benchmark
        where large orders cluster. Proven statistical significance in
        Person (2004) and Grimes (2012).

        Uses PRIOR day's H/L/C to compute today's levels.
        Returns positive when price is at support (bounce expected).
        """
        if len(df) < 3:
            return np.nan

        # Prior day's values
        prev = df.iloc[-2]
        prev_h, prev_l, prev_c = prev["high"], prev["low"], prev["close"]
        price = df["close"].iloc[-1]

        pp = (prev_h + prev_l + prev_c) / 3.0
        s1 = 2.0 * pp - prev_h
        r1 = 2.0 * pp - prev_l
        s2 = pp - (prev_h - prev_l)
        r2 = pp + (prev_h - prev_l)

        levels = [s2, s1, pp, r1, r2]
        pivot_range = r2 - s2
        if pivot_range < 1e-10:
            return 0.0

        # Distance to nearest level
        distances = [(abs(price - lv) / pivot_range, lv) for lv in levels]
        min_dist, nearest = min(distances, key=lambda x: x[0])

        proximity = max(0.0, 1.0 - min_dist * 4.0)

        # At support = bullish, at resistance = bearish
        if price <= pp:
            return float(proximity)  # Near support = buy
        else:
            return float(-proximity)  # Near resistance = sell

    @staticmethod
    def keltner_position(df: pd.DataFrame, ema_period: int = 20, atr_mult: float = 1.5,
                          atr_period: int = 14) -> float:
        """
        Position within Keltner Channel. Keltner 1960.
        Unlike Bollinger (standard deviation), Keltner uses ATR.
        This captures DIFFERENT volatility information:
        - Bollinger responds to close-to-close variance
        - Keltner responds to true range (includes gaps and wicks)

        Returns [-1, +1]: below channel = oversold (positive for MR),
        above channel = overbought (negative for MR).
        """
        c = df["close"]
        h, l = df["high"], df["low"]
        ema = c.ewm(span=ema_period, adjust=False).mean()

        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(atr_period).mean()

        upper = ema + atr_mult * atr
        lower = ema - atr_mult * atr
        mid = ema

        width = upper.iloc[-1] - lower.iloc[-1]
        if width < 1e-10:
            return 0.0

        position = (c.iloc[-1] - mid.iloc[-1]) / (width / 2.0)
        return float(-np.clip(position, -2.0, 2.0) / 2.0)

    @staticmethod
    def ichimoku_signal(df: pd.DataFrame) -> float:
        """
        Ichimoku Kinfo composite. Hosoda 1968.
        Combines Tenkan-sen / Kijun-sen crossover + cloud position.

        5-in-1 system capturing trend, momentum, and support/resistance
        in a single framework. Validated for swing trading in
        Pongsapan & Termprasertsakul (2016).

        Returns [-1, +1]:
          +1 = bullish (TK cross up, price above cloud)
          -1 = bearish (TK cross down, price below cloud)
        """
        h, l, c = df["high"], df["low"], df["close"]
        if len(df) < 52:
            return np.nan

        # Tenkan-sen (conversion line): midpoint of 9-period range
        tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2.0

        # Kijun-sen (base line): midpoint of 26-period range
        kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2.0

        # Senkou Span A (leading span A): midpoint of Tenkan and Kijun
        span_a = (tenkan + kijun) / 2.0

        # Senkou Span B (leading span B): midpoint of 52-period range
        span_b = (h.rolling(52).max() + l.rolling(52).min()) / 2.0

        price = c.iloc[-1]
        tenkan_now = tenkan.iloc[-1]
        kijun_now = kijun.iloc[-1]
        span_a_now = span_a.iloc[-1]
        span_b_now = span_b.iloc[-1]
        cloud_top = max(span_a_now, span_b_now)
        cloud_bottom = min(span_a_now, span_b_now)

        score = 0.0

        # TK cross: Tenkan above Kijun = bullish
        if tenkan_now > kijun_now:
            score += 0.4
        else:
            score -= 0.4

        # Price vs cloud
        if price > cloud_top:
            score += 0.4  # Above cloud = bullish
        elif price < cloud_bottom:
            score -= 0.4  # Below cloud = bearish
        # else: inside cloud = neutral

        # Cloud color: Span A > Span B = bullish cloud
        if span_a_now > span_b_now:
            score += 0.2
        else:
            score -= 0.2

        return float(np.clip(score, -1.0, 1.0))

    # ── VOLUME & FLOW (4 factors) ────────────────────────────────────────────

    @staticmethod
    def obv_r2(df: pd.DataFrame, lb: int = 10) -> float:
        """OBV regression slope * R-squared. Granville 1963."""
        obv = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
        y = obv.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * r ** 2)

    @staticmethod
    def vpt_slope(df: pd.DataFrame, lb: int = 10) -> float:
        """
        Volume-Price Trend regression slope. Markstein 1966.
        VPT = cumsum(volume * pct_change). Unlike OBV which only uses
        direction (+1/-1), VPT weights by the MAGNITUDE of the move.
        A +3% day with 1M shares contributes 6x more than a +0.5% day.
        This captures conviction — large moves on volume = institutional.
        """
        pct = df["close"].pct_change()
        vpt = (pct * df["volume"]).cumsum()
        y = vpt.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * abs(r))

    @staticmethod
    def accum_dist(df: pd.DataFrame, lb: int = 10) -> float:
        """Chaikin A/D slope * |R|. Close-location-value weighting."""
        clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-10)
        ad = (clv * df["volume"]).cumsum()
        y = ad.iloc[-lb:].values
        y_s = (y - y.mean()) / (y.std() + 1e-10)
        s, _, r, _, _ = scipy_stats.linregress(np.arange(lb, dtype=float), y_s)
        return float(s * abs(r))

    @staticmethod
    def mfi_signal(df: pd.DataFrame, period: int = 14) -> float:
        """
        Money Flow Index. Quong & Soudack 1989.
        Volume-weighted RSI — combines price momentum with volume.
        Not redundant with RSI because the volume weighting changes
        which days dominate the calculation.
        Returns [-1, +1]: oversold = positive.
        """
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        raw_mf = tp * df["volume"]
        delta_tp = tp.diff()

        pos_mf = raw_mf.where(delta_tp > 0, 0.0).rolling(period).sum()
        neg_mf = raw_mf.where(delta_tp < 0, 0.0).rolling(period).sum()

        mfi = 100.0 - 100.0 / (1.0 + pos_mf / (neg_mf + 1e-10))
        return float((50.0 - mfi.iloc[-1]) / 50.0)

    # ── VOLATILITY (2 factors) ───────────────────────────────────────────────

    @staticmethod
    def atr_pctile(df: pd.DataFrame, atr_p: int = 14, lb: int = 60) -> float:
        """ATR percentile of 60-day distribution. High vol = negative."""
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.rolling(atr_p).mean().dropna()
        if len(atr_s) < lb:
            return np.nan
        pct = scipy_stats.percentileofscore(atr_s.iloc[-lb:].values, atr_s.iloc[-1]) / 100.0
        return float(-(pct - 0.5) * 2.0)

    @staticmethod
    def bb_squeeze(c: pd.Series, period: int = 20, lb: int = 60) -> float:
        """Bollinger bandwidth percentile. Squeeze = positive."""
        bw = (4.0 * c.rolling(period).std() / c.rolling(period).mean()).dropna()
        if len(bw) < lb:
            return np.nan
        pct = scipy_stats.percentileofscore(bw.iloc[-lb:].values, bw.iloc[-1]) / 100.0
        return float(-(pct - 0.5) * 2.0)

    # ── RELATIVE VALUE (1 factor) ────────────────────────────────────────────

    @staticmethod
    def rel_strength(sym_c: pd.Series, spy_c: pd.Series, period: int = 10) -> float:
        """10-day return vs SPY. Levy 1967."""
        if len(spy_c) < period:
            return np.nan
        return float((sym_c.iloc[-1] / sym_c.iloc[-period]) -
                     (spy_c.iloc[-1] / spy_c.iloc[-period]))

    # ── UTILITY ──────────────────────────────────────────────────────────────

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> float:
        h, l, c = df["high"], df["low"], df["close"]
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE V6
# ═══════════════════════════════════════════════════════════════════════════════

class QuantSignalEngineV6:
    """
    20 stock-level TA factors. 6 clusters. Macro as regime multipliers.
    Cross-sectional z-score -> orthogonalize -> regime-weight -> t-test.
    """

    # (name, cluster, base_weight) — MUST sum to 1.0
    FACTORS = [
        # Mean-reversion (30%)
        ("rsi_mr",          "mr",   0.07),
        ("bollinger_z",     "mr",   0.06),
        ("williams_r",      "mr",   0.05),
        ("ma_deviation",    "mr",   0.06),
        ("hurst",           "mr",   0.06),
        # Momentum & trend (22%)
        ("ma_stack",        "trend", 0.06),
        ("macd_accel",      "trend", 0.06),
        ("adx_signed",      "trend", 0.05),
        ("roc_cascade",     "trend", 0.05),
        # Support/resistance (15%)
        ("fib_proximity",   "sr",   0.04),
        ("pivot_proximity", "sr",   0.04),
        ("keltner_pos",     "sr",   0.04),
        ("ichimoku",        "sr",   0.03),
        # Volume & flow (15%)
        ("obv_r2",          "flow", 0.04),
        ("vpt_slope",       "flow", 0.04),
        ("accum_dist",      "flow", 0.04),
        ("mfi_signal",      "flow", 0.03),
        # Volatility (10%)
        ("atr_pctile",      "vol",  0.05),
        ("bb_squeeze",      "vol",  0.05),
        # Relative value (8%)
        ("rel_strength",    "rel",  0.08),
    ]

    REGIME_MULT = {
        "EXPANSION": {"mr": 0.8, "trend": 1.3, "sr": 1.0, "flow": 1.0, "vol": 0.8, "rel": 1.1},
        "BULLISH":   {"mr": 0.9, "trend": 1.2, "sr": 1.0, "flow": 1.0, "vol": 0.9, "rel": 1.0},
        "NEUTRAL":   {"mr": 1.0, "trend": 1.0, "sr": 1.0, "flow": 1.0, "vol": 1.0, "rel": 1.0},
        "BEARISH":   {"mr": 1.3, "trend": 0.7, "sr": 1.2, "flow": 1.1, "vol": 1.2, "rel": 0.9},
        "CRISIS":    {"mr": 1.5, "trend": 0.5, "sr": 1.3, "flow": 1.2, "vol": 1.4, "rel": 0.7},
    }

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.rcfg = cfg.risk
        self.f = FactorsV6()
        total = sum(w for _, _, w in self.FACTORS)
        assert abs(total - 1.0) < 0.001, f"Factor weights sum to {total}, expected 1.0"
        logger.info("QuantSignalEngineV6 initialized: %d factors, %d clusters",
                     len(self.FACTORS), len(set(c for _, c, _ in self.FACTORS)))

    def _raw(self, sym, bars, spy_bars):
        """Compute all 20 raw factor values."""
        c, v = bars["close"], bars["volume"]
        spy_c = spy_bars["close"] if spy_bars is not None else pd.Series(dtype=float)
        return {
            "rsi_mr":          self.f.rsi_mr(c),
            "bollinger_z":     self.f.bollinger_z(c),
            "williams_r":      self.f.williams_r(bars),
            "ma_deviation":    self.f.ma_deviation(c),
            "hurst":           self.f.hurst(c),
            "ma_stack":        self.f.ma_stack(c),
            "macd_accel":      self.f.macd_accel(c),
            "adx_signed":      self.f.adx_signed(bars),
            "roc_cascade":     self.f.roc_cascade(c),
            "fib_proximity":   self.f.fib_proximity(bars),
            "pivot_proximity": self.f.pivot_proximity(bars),
            "keltner_pos":     self.f.keltner_position(bars),
            "ichimoku":        self.f.ichimoku_signal(bars),
            "obv_r2":          self.f.obv_r2(bars),
            "vpt_slope":       self.f.vpt_slope(bars),
            "accum_dist":      self.f.accum_dist(bars),
            "mfi_signal":      self.f.mfi_signal(bars),
            "atr_pctile":      self.f.atr_pctile(bars),
            "bb_squeeze":      self.f.bb_squeeze(c),
            "rel_strength":    self.f.rel_strength(c, spy_c),
        }

    def _zscore(self, raw_matrix):
        syms = list(raw_matrix.keys())
        names = [n for n, _, _ in self.FACTORS]
        out = {s: {} for s in syms}
        for fn in names:
            valid = [(s, raw_matrix[s][fn]) for s in syms
                     if not (isinstance(raw_matrix[s][fn], float) and np.isnan(raw_matrix[s][fn]))]
            if len(valid) < 5:
                for s in syms:
                    out[s][fn] = 0.0
                continue
            arr = np.array([v for _, v in valid])
            mu, sigma = arr.mean(), arr.std()
            if sigma < 1e-10:
                for s in syms:
                    out[s][fn] = 0.0
                continue
            valid_set = set()
            for s, val in valid:
                out[s][fn] = float(np.clip((val - mu) / sigma, -3, 3))
                valid_set.add(s)
            for s in syms:
                if s not in valid_set:
                    out[s][fn] = 0.0
        return out

    def _orthogonalize(self, zmat):
        syms = list(zmat.keys())
        clusters = {}
        for fn, cl, _ in self.FACTORS:
            clusters.setdefault(cl, []).append(fn)
        ort = {s: dict(zmat[s]) for s in syms}
        for cl, flist in clusters.items():
            if len(flist) <= 1:
                continue
            n, k = len(syms), len(flist)
            M = np.zeros((n, k))
            for j, fn in enumerate(flist):
                for i, s in enumerate(syms):
                    M[i, j] = zmat[s][fn]
            for j in range(1, k):
                X, y = M[:, :j], M[:, j]
                try:
                    beta = np.linalg.lstsq(X, y, rcond=None)[0]
                    res = y - X @ beta
                    rs = res.std()
                    if rs > 1e-10:
                        res /= rs
                    M[:, j] = res
                except np.linalg.LinAlgError:
                    pass
            for j, fn in enumerate(flist):
                for i, s in enumerate(syms):
                    ort[s][fn] = float(M[i, j])
        return ort

    def _weights(self, regime):
        mult = self.REGIME_MULT.get(regime, self.REGIME_MULT["NEUTRAL"])
        adj = {fn: w * mult[cl] for fn, cl, w in self.FACTORS}
        tot = sum(adj.values())
        return {k: v / tot for k, v in adj.items()}

    def _score(self, ort_scores, weights):
        comp, var, contribs = 0.0, 0.0, {}
        for fn in ort_scores:
            w = weights.get(fn, 0)
            contrib = w * ort_scores[fn]
            comp += contrib
            contribs[fn] = round(contrib, 6)
            var += w ** 2
        t = comp / np.sqrt(var) if var > 0 else 0.0
        return comp, t, contribs

    @staticmethod
    def _hold_from_atr(atr_pctile_val):
        if isinstance(atr_pctile_val, float) and np.isnan(atr_pctile_val):
            return 10
        vol = -atr_pctile_val
        return max(1, min(30, int(16 - 14 * vol)))

    def generate_signals(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime = macro_data.get("regime", "NEUTRAL")
        if regime == "CRISIS" and self.rcfg.halt_in_crisis:
            logger.warning("CRISIS - halting entries")
            return []

        raw = {}
        for sym, bars in bars_dict.items():
            if len(bars) < MIN_BARS_REQUIRED:
                continue
            try:
                raw[sym] = self._raw(sym, bars, spy_bars)
            except Exception as e:
                logger.error("Factor error %s: %s", sym, e)

        if len(raw) < 10:
            logger.warning("Only %d symbols with sufficient data", len(raw))
            return []

        zs = self._zscore(raw)
        ort = self._orthogonalize(zs)
        w = self._weights(regime)

        candidates = []
        all_comp = []
        for sym in ort:
            comp, t, contribs = self._score(ort[sym], w)
            all_comp.append(comp)
            candidates.append((sym, comp, t, contribs))

        comp_arr = np.array(all_comp)
        signals = []

        for sym, comp, t, contribs in candidates:
            if t < 1.65:
                continue

            pctile = scipy_stats.percentileofscore(comp_arr, comp) / 100.0
            bars = bars_dict[sym]
            entry = float(bars["close"].iloc[-1])
            atr_val = self.f.atr(bars, self.rcfg.atr_period)
            if atr_val <= 0:
                continue

            stop = round(entry - self.rcfg.initial_stop_atr_mult * atr_val, 2)
            tp = round(entry + self.rcfg.take_profit_atr_mult * atr_val, 2)

            edge = max(0, pctile - 0.5) * 2
            risk = (entry - stop) / entry
            if risk > 0:
                kelly = min(edge / risk * self.rcfg.kelly_fraction, self.rcfg.max_position_pct)
                kelly = max(kelly, self.rcfg.min_position_pct)
            else:
                kelly = self.rcfg.min_position_pct
            if regime == "BEARISH":
                kelly *= self.rcfg.reduce_in_bearish

            atr_p = raw[sym].get("atr_pctile", 0)
            hold = self._hold_from_atr(atr_p)
            fscores = {k: round(v, 4) for k, v in ort[sym].items()}

            signals.append(Signal(
                symbol=sym, side="BUY",
                composite_score=round(comp, 4),
                composite_percentile=round(pctile, 4),
                t_statistic=round(t, 4),
                factor_scores=fscores,
                factor_contributions=contribs,
                factors_positive=sum(1 for v in fscores.values() if v > 0),
                regime=regime, sentiment_score=0.0,
                atr=round(atr_val, 4), entry_price=entry,
                stop_price=stop, take_profit=tp,
                kelly_fraction=round(kelly, 4),
                hold_target_days=hold, timestamp=str(bars.index[-1]),
            ))

        signals.sort(key=lambda s: s.t_statistic, reverse=True)
        if len(signals) > self.cfg.execution.max_orders_per_scan:
            signals = signals[:self.cfg.execution.max_orders_per_scan]

        logger.info("v6 Signals: %d from %d syms (regime=%s)", len(signals), len(raw), regime)
        if len(comp_arr):
            logger.info("v6 Composite: mean=%.4f std=%.4f max=%.4f", comp_arr.mean(), comp_arr.std(), comp_arr.max())
        return signals

    def get_diagnostics(self, bars_dict, macro_data, sentiment_dict, spy_bars=None):
        regime = macro_data.get("regime", "NEUTRAL")
        w = self._weights(regime)
        bar_counts = {s: len(b) for s, b in bars_dict.items()}
        sufficient = sum(1 for v in bar_counts.values() if v >= MIN_BARS_REQUIRED)
        insufficient = sum(1 for v in bar_counts.values() if v < MIN_BARS_REQUIRED)

        raw = {}
        for sym, bars in bars_dict.items():
            if len(bars) < MIN_BARS_REQUIRED:
                continue
            try:
                raw[sym] = self._raw(sym, bars, spy_bars)
            except Exception:
                pass

        nan_counts = {n: 0 for n, _, _ in self.FACTORS}
        for sym in raw:
            for fn in nan_counts:
                val = raw[sym].get(fn, np.nan)
                if isinstance(val, float) and np.isnan(val):
                    nan_counts[fn] += 1

        zs = self._zscore(raw)
        ort = self._orthogonalize(zs)

        results = []
        for sym in ort:
            comp, t, contribs = self._score(ort[sym], w)
            atr_p = raw.get(sym, {}).get("atr_pctile", 0)
            hold = self._hold_from_atr(atr_p)
            results.append({"symbol": sym, "composite": comp, "t_stat": t,
                            "contributions": contribs, "raw": raw.get(sym, {}),
                            "ortho": ort.get(sym, {}), "hold": hold,
                            "bars": bar_counts.get(sym, 0)})

        results.sort(key=lambda r: r["t_stat"], reverse=True)
        comps = [r["composite"] for r in results]
        ts = [r["t_stat"] for r in results]
        passing = sum(1 for t in ts if t > 1.65)

        L = []
        L.append("=" * 80)
        L.append("  RAPTOR v6.0 ADVANCED TA ENGINE DIAGNOSTICS")
        L.append("=" * 80)
        L.append(f"  Strategy: Swing trades | 1d-6wk | Pure TA + math")
        L.append(f"  Regime: {regime} (macro={macro_data.get('score', 0):.3f})")
        L.append(f"  Universe: {len(bars_dict)} fetched | {sufficient} sufficient"
                 f" | {insufficient} skipped (<{MIN_BARS_REQUIRED} bars)")
        L.append(f"  Factors: {len(self.FACTORS)} (20 TA) | 6 clusters")
        L.append(f"  Threshold: t > 1.65 (95th percentile)")
        L.append("")
        L.append("  DATA QUALITY:")
        for fn, _, _ in self.FACTORS:
            nc = nan_counts.get(fn, 0)
            status = "OK" if nc == 0 else f"WARN {nc} NaN"
            L.append(f"    {fn:20s} {status}")
        L.append("")
        L.append("  FACTOR WEIGHTS (regime-adjusted):")
        for fn, cl, bw in self.FACTORS:
            aw = w.get(fn, 0)
            L.append(f"    {fn:20s} [{cl:5s}]  base={bw:.2f}  adj={aw:.4f}")
        L.append("")
        L.append(f"  Composite: mean={np.mean(comps):.4f}  std={np.std(comps):.4f}")
        L.append(f"  T-stats:   mean={np.mean(ts):.3f}  max={max(ts):.3f}  min={min(ts):.3f}")
        L.append(f"  Signals passing: {passing}")
        L.append("")
        L.append("-" * 80)
        L.append("  TOP 15 BY T-STATISTIC")
        L.append("-" * 80)
        for r in results[:15]:
            status = "PASS" if r["t_stat"] > 1.65 else "FAIL"
            L.append(f"\n  {r['symbol']:6s}  t={r['t_stat']:+.3f}  comp={r['composite']:+.4f}"
                     f"  hold~{r['hold']}d  bars={r['bars']}  [{status}]")
            cs = sorted(r["contributions"].items(), key=lambda x: -abs(x[1]))
            for fn, contrib in cs:
                z = r["ortho"].get(fn, 0)
                rv = r["raw"].get(fn, 0)
                rv = rv if not (isinstance(rv, float) and np.isnan(rv)) else 0.0
                d = "+" if contrib > 0 else "-" if contrib < 0 else " "
                L.append(f"      {fn:20s} raw={rv:+9.4f}  z={z:+6.3f}"
                         f"  w={w[fn]:.4f}  c={contrib:+.6f} {d}")
        L.append("")
        L.append("-" * 80)
        L.append("  BOTTOM 5")
        L.append("-" * 80)
        for r in results[-5:]:
            L.append(f"  {r['symbol']:6s}  t={r['t_stat']:+.3f}  comp={r['composite']:+.4f}")
        L.append("")
        L.append("=" * 80)
        return "\n".join(L)
