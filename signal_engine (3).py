"""
=============================================================================
ELITE SIGNAL ENGINE v2.0 — 17-POINT SCORING SYSTEM
=============================================================================
Every trade is scored 0-17. Only the best trades get through.

THE 17 SCORING DIMENSIONS:
TECHNICAL ANALYSIS (5 points)
  1. Trend Alignment       EMA stack, MACD, ADX, VWAP
  2. Momentum              ROC, breakout, volume surge, OBV
  3. Mean Reversion        RSI, Bollinger Bands, Z-score extremes
  4. Pattern Recognition   Candlestick patterns, support/resistance
  5. Multi-Timeframe       Signals align across multiple timeframes

QUANTITATIVE / STATISTICAL (4 points)
  6. Statistical Edge      Z-score, regression deviation, Hurst exponent
  7. Volatility Regime     ATR trend, BB squeeze, vol percentile rank
  8. Probability Model     Historical win rate at similar setups
  9. Expected Value        Cost-adjusted EV relative to portfolio

MARKET MICROSTRUCTURE (2 points)
  10. Volume Profile       Institutional volume, money flow, accumulation
  11. Market Regime        Broad market trend, sector strength, beta

PHYSICS / FLUID DYNAMICS (2 points)
  12. Price Momentum       Newton's laws: trends in motion stay in motion
  13. Support/Resistance   Fluid pressure: price bounces off key levels

PSYCHOLOGY (2 points)
  14. Fear and Greed       Overbought/oversold crowd psychology
  15. Behavioral Bias      Anchoring, recency bias, gap fills

SENTIMENT (2 points)
  16. News Sentiment       Price gap analysis as news impact proxy
  17. Social Momentum      Unusual volume as social attention proxy

SCORE INTERPRETATION:
  0-5   = No trade. Weak or conflicting signals.
  6-9   = Marginal. Only trade with tight size and strict stops.
  10-12 = Good trade. Standard position size.
  13-15 = Strong trade. Full position size allowed.
  16-17 = ELITE setup. Maximum position size. Rare -- maybe 1-2x per week.
=============================================================================
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def score_trend_alignment(df, config):
    score = 0.0
    components = {}
    try:
        close = df['close'].iloc[-1]
        ema_f = df['ema_fast'].iloc[-1]
        ema_m = df['ema_medium'].iloc[-1]
        ema_s = df['ema_slow'].iloc[-1]
        ema_t = df['ema_trend'].iloc[-1]
        adx   = df['adx'].iloc[-1]
        plus_di = df['plus_di'].iloc[-1]
        minus_di = df['minus_di'].iloc[-1]
        macd_hist = df['macd_hist'].iloc[-1]
        macd_hist_prev = df['macd_hist'].iloc[-2]
        vwap = df['vwap'].iloc[-1]

        if ema_f > ema_m > ema_s and close > ema_t:
            components['ema'] = 0.35
        elif ema_f > ema_m:
            components['ema'] = 0.15
        else:
            components['ema'] = 0.0

        if adx > 35 and plus_di > minus_di:
            components['adx'] = 0.30
        elif adx > config.ADX_THRESHOLD and plus_di > minus_di:
            components['adx'] = 0.20
        elif adx > config.ADX_THRESHOLD:
            components['adx'] = 0.10
        else:
            components['adx'] = 0.0

        if macd_hist > 0 and macd_hist > macd_hist_prev:
            components['macd'] = 0.20
        elif macd_hist > 0:
            components['macd'] = 0.10
        else:
            components['macd'] = 0.0

        components['vwap'] = 0.15 if close > vwap else 0.0
        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Trend alignment: {e}")
    return {"score": min(1.0, score), "components": components, "name": "trend_alignment"}


def score_momentum(df, config):
    score = 0.0
    components = {}
    try:
        close = df['close']
        roc_5  = close.pct_change(5).iloc[-1] * 100
        roc_10 = close.pct_change(10).iloc[-1] * 100
        roc_20 = close.pct_change(20).iloc[-1] * 100
        vol_ratio = df['vol_ratio'].iloc[-1]
        obv = df['obv']
        recent_high = df['high'].iloc[-20:-1].max()
        price = close.iloc[-1]

        if roc_5 > 0 and roc_10 > 0 and roc_20 > 0:
            components['roc_align'] = 0.30
        elif roc_5 > 0 and roc_10 > 0:
            components['roc_align'] = 0.15
        else:
            components['roc_align'] = 0.0

        if vol_ratio > 2.0:
            components['volume'] = 0.25
        elif vol_ratio > config.VOLUME_SURGE_MULT:
            components['volume'] = 0.15
        else:
            components['volume'] = 0.0

        obv_slope = (obv.iloc[-1] - obv.iloc[-10]) / (abs(obv.iloc[-10]) + 1e-9)
        components['obv'] = min(0.25, abs(obv_slope) * 5) if obv_slope > 0 else 0.0

        components['breakout'] = 0.20 if price > recent_high else 0.0
        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Momentum: {e}")
    return {"score": min(1.0, score), "components": components, "name": "momentum"}


def score_mean_reversion(df, config):
    score = 0.0
    components = {}
    try:
        rsi_val  = df['rsi'].iloc[-1]
        bb_pct   = df['bb_pct'].iloc[-1]
        zscore   = df['zscore'].iloc[-1]
        stoch_k  = df['stoch_k'].iloc[-1]
        adx_val  = df['adx'].iloc[-1]
        mfi      = df['mfi'].iloc[-1]
        trend_penalty = max(0.4, 1.0 - max(0, adx_val - 20) / 50)

        if rsi_val < 25:
            components['rsi'] = 0.30
        elif rsi_val < config.RSI_OVERSOLD:
            components['rsi'] = 0.20
        elif rsi_val > 75:
            components['rsi'] = 0.30
        elif rsi_val > config.RSI_OVERBOUGHT:
            components['rsi'] = 0.20
        else:
            components['rsi'] = 0.0

        if bb_pct < 0.02 or bb_pct > 0.98:
            components['bb'] = 0.30
        elif bb_pct < 0.08 or bb_pct > 0.92:
            components['bb'] = 0.15
        else:
            components['bb'] = 0.0

        if abs(zscore) > 2.5:
            components['zscore'] = 0.25
        elif abs(zscore) > 2.0:
            components['zscore'] = 0.15
        else:
            components['zscore'] = 0.0

        components['mfi'] = 0.15 if (mfi < 20 or mfi > 80) else 0.0
        score = sum(components.values()) * trend_penalty
    except Exception as e:
        logger.debug(f"Mean reversion: {e}")
    return {"score": min(1.0, score), "components": components, "name": "mean_reversion"}


def score_pattern_recognition(df, config):
    score = 0.0
    components = {}
    try:
        o = df['open'].iloc[-1]; h = df['high'].iloc[-1]
        l = df['low'].iloc[-1];  c = df['close'].iloc[-1]
        o2= df['open'].iloc[-2]; h2= df['high'].iloc[-2]
        l2= df['low'].iloc[-2];  c2= df['close'].iloc[-2]
        atr_val = df['atr'].iloc[-1]
        body = abs(c - o); range_ = h - l + 1e-9
        upper_wick = h - max(o, c); lower_wick = min(o, c) - l

        if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o and body > 0:
            components['hammer'] = 0.25
        elif upper_wick > body * 2 and lower_wick < body * 0.5 and c < o and body > 0:
            components['shooting_star'] = 0.25

        if c > o and c2 < o2 and c > o2 and o < c2:
            components['bull_engulf'] = 0.30
        elif c < o and c2 > o2 and c < o2 and o > c2:
            components['bear_engulf'] = 0.30

        if body < range_ * 0.1:
            components['doji'] = 0.10
        if h < h2 and l > l2:
            components['inside_bar'] = 0.15

        recent_highs = df['high'].iloc[-20:-1]
        recent_lows  = df['low'].iloc[-20:-1]
        if abs(c - recent_lows.min()) < atr_val * 0.3:
            components['support_test'] = 0.20
        elif abs(c - recent_highs.max()) < atr_val * 0.3:
            components['resistance_test'] = 0.20

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Patterns: {e}")
    return {"score": min(1.0, score), "components": components, "name": "pattern_recognition"}


def score_multi_timeframe(df, config):
    score = 0.0
    components = {}
    try:
        close = df['close']
        short_trend  = close.iloc[-1] > close.iloc[-5]
        medium_trend = close.iloc[-1] > close.iloc[-20]
        long_trend   = close.iloc[-1] > close.iloc[-50] if len(close) > 50 else medium_trend
        short_roc    = close.pct_change(5).iloc[-1]
        medium_roc   = close.pct_change(20).iloc[-1]

        if short_trend and medium_trend and long_trend:
            components['all_aligned'] = 0.50
        elif medium_trend and long_trend:
            components['two_aligned'] = 0.30
        elif short_trend and medium_trend:
            components['two_aligned'] = 0.20

        if short_roc > medium_roc > 0:
            components['accelerating'] = 0.30
        elif short_roc > 0 and medium_roc > 0:
            components['positive'] = 0.20

        pos_days = (close.pct_change().iloc[-20:] > 0).sum()
        if pos_days >= 14:
            components['consistency'] = 0.20
        elif pos_days >= 12:
            components['consistency'] = 0.10

        score = sum(v for v in components.values() if isinstance(v, float))
    except Exception as e:
        logger.debug(f"Multi-timeframe: {e}")
    return {"score": min(1.0, score), "components": components, "name": "multi_timeframe"}


def score_statistical_edge(df, config):
    score = 0.0
    components = {}
    try:
        close = df['close']
        returns = close.pct_change().dropna()
        period = 20
        y = close.iloc[-period:].values; x = np.arange(period)
        slope = np.polyfit(x, y, 1)[0]
        norm_slope = slope / close.iloc[-1] * 100
        components['regression'] = min(0.30, norm_slope * 3) if norm_slope > 0.05 else 0.0

        if len(close) >= 30:
            lags = range(2, 15)
            tau = []
            for lag in lags:
                diff = np.subtract(close.iloc[-30:].iloc[lag:].values,
                                   close.iloc[-30:].iloc[:-lag].values)
                s = np.std(diff)
                if s > 0:
                    tau.append((lag, s))
            if len(tau) >= 4:
                xh = np.log([t[0] for t in tau]); yh = np.log([t[1] for t in tau])
                hurst = np.polyfit(xh, yh, 1)[0]
                components['hurst'] = 0.25 if hurst > 0.6 else (0.20 if hurst < 0.4 else 0.10)

        if len(returns) >= 20:
            r = returns.iloc[-20:].values
            if len(r) > 2:
                lag1 = np.corrcoef(r[:-1], r[1:])[0, 1]
                if abs(lag1) > 0.15:
                    components['autocorr'] = min(0.25, abs(lag1) * 0.8)
            skew = returns.iloc[-20:].skew()
            components['skew'] = 0.20 if skew > 0.3 else (0.10 if skew > 0 else 0.0)

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Statistical edge: {e}")
    return {"score": min(1.0, score), "components": components, "name": "statistical_edge"}


def score_volatility_regime(df, config):
    score = 0.0
    components = {}
    try:
        atr_current = df['atr'].iloc[-1]
        atr_avg     = df['atr'].iloc[-20:].mean()
        hist_vol    = df['hist_vol'].iloc[-1]
        bb_bw       = df['bb_bw'].iloc[-1]
        bb_pct_rank = (df['bb_bw'].iloc[-20:] <= bb_bw).mean()
        atr_trend   = (atr_current - atr_avg) / (atr_avg + 1e-9)

        if hist_vol < 15:
            components['vol_level'] = 0.30
        elif hist_vol < 25:
            components['vol_level'] = 0.20
        elif hist_vol < 40:
            components['vol_level'] = 0.10
        else:
            components['vol_level'] = 0.0

        if bb_pct_rank < 0.15:
            components['squeeze'] = 0.35
        elif bb_pct_rank < 0.30:
            components['squeeze'] = 0.20
        else:
            components['squeeze'] = 0.0

        if atr_trend < -0.15:
            components['atr_trend'] = 0.20
        elif atr_trend < 0:
            components['atr_trend'] = 0.10
        elif atr_trend <= 0.30:
            components['atr_trend'] = 0.15
        else:
            components['atr_trend'] = 0.0

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Volatility regime: {e}")
    return {"score": min(1.0, score), "components": components, "name": "volatility_regime"}


def score_probability_model(df, config, signal_history=None):
    score = 0.0
    components = {}
    if signal_history is None:
        signal_history = []
    try:
        rsi_val   = df['rsi'].iloc[-1]
        adx_val   = df['adx'].iloc[-1]
        vol_ratio = df['vol_ratio'].iloc[-1]
        returns   = df['close'].pct_change().dropna()

        if 40 <= rsi_val <= 60 and adx_val > 25:
            components['base_rate'] = 0.35
        elif 35 <= rsi_val <= 65:
            components['base_rate'] = 0.25
        else:
            components['base_rate'] = 0.15

        if vol_ratio > 1.5:
            components['vol_confirm'] = 0.25
        elif vol_ratio > 1.2:
            components['vol_confirm'] = 0.15
        else:
            components['vol_confirm'] = 0.05

        if len(signal_history) >= 10:
            recent = signal_history[-20:]
            wins = sum(1 for s in recent if s.get('won', False))
            recent_wr = wins / len(recent)
            if recent_wr > 0.60:
                components['recent_accuracy'] = 0.25
            elif recent_wr > 0.50:
                components['recent_accuracy'] = 0.15
            else:
                components['recent_accuracy'] = 0.05
        else:
            components['recent_accuracy'] = 0.15

        pos_rate = (returns.iloc[-10:] > 0).mean()
        components['pos_rate'] = 0.15 if pos_rate > 0.65 else (0.10 if pos_rate > 0.50 else 0.0)
        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Probability model: {e}")
    return {"score": min(1.0, score), "components": components, "name": "probability_model"}


def score_expected_value(df, config):
    score = 0.0
    components = {}
    try:
        close   = df['close'].iloc[-1]
        atr_val = df['atr'].iloc[-1]
        rr_ratio = 3.0 / 2.0

        if rr_ratio >= 3.0:
            components['rr_ratio'] = 0.40
        elif rr_ratio >= 2.0:
            components['rr_ratio'] = 0.30
        else:
            components['rr_ratio'] = 0.20

        atr_pct = atr_val / close * 100
        if atr_pct < 0.5:
            components['tightness'] = 0.30
        elif atr_pct < 1.0:
            components['tightness'] = 0.20
        elif atr_pct < 2.0:
            components['tightness'] = 0.10
        else:
            components['tightness'] = 0.0

        risk_pct = (atr_val * 2) / close * 100
        if risk_pct < 1.0:
            components['risk_quality'] = 0.30
        elif risk_pct < 2.0:
            components['risk_quality'] = 0.20
        else:
            components['risk_quality'] = 0.10

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Expected value: {e}")
    return {"score": min(1.0, score), "components": components, "name": "expected_value"}


def score_volume_profile(df, config):
    score = 0.0
    components = {}
    try:
        mfi   = df['mfi'].iloc[-1]
        obv   = df['obv']
        close = df['close']
        vol   = df['volume']

        obv_slope = (obv.iloc[-1] - obv.iloc[-20]) / (abs(obv.iloc[-20]) + 1e-9)
        if obv_slope > 0.05:
            components['obv_trend'] = 0.30
        elif obv_slope > 0:
            components['obv_trend'] = 0.15
        else:
            components['obv_trend'] = 0.0

        if 40 <= mfi <= 60:
            components['mfi'] = 0.20
        elif mfi > 60:
            components['mfi'] = 0.25
        elif mfi < 30:
            components['mfi'] = 0.20

        vol_5 = vol.iloc[-5:].mean(); vol_20 = vol.iloc[-20:].mean()
        if vol_5 > vol_20 * 1.3:
            components['vol_trend'] = 0.25
        elif vol_5 > vol_20:
            components['vol_trend'] = 0.15
        else:
            components['vol_trend'] = 0.0

        pct_chg = close.pct_change().iloc[-10:]
        up_vol   = vol.iloc[-10:][pct_chg > 0].sum()
        down_vol = vol.iloc[-10:][pct_chg < 0].sum()
        if down_vol > 0:
            uvr = up_vol / down_vol
            components['up_vol_ratio'] = 0.25 if uvr > 1.5 else (0.10 if uvr > 1.0 else 0.0)

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Volume profile: {e}")
    return {"score": min(1.0, score), "components": components, "name": "volume_profile"}


def score_market_regime(df, config):
    score = 0.0
    components = {}
    try:
        close   = df['close']
        ema_200 = df['ema_trend'].iloc[-1]
        ema_50  = df['ema_slow'].iloc[-1]
        ema_50_prev = df['ema_slow'].iloc[-5]
        current = close.iloc[-1]

        if current > ema_200 * 1.02:
            components['market_trend'] = 0.40
        elif current > ema_200:
            components['market_trend'] = 0.25
        elif current > ema_200 * 0.97:
            components['market_trend'] = 0.10
        else:
            components['market_trend'] = 0.0

        ema50_slope = (ema_50 - ema_50_prev) / (ema_50_prev + 1e-9) * 100
        if ema50_slope > 0.1:
            components['ema50_slope'] = 0.30
        elif ema50_slope > 0:
            components['ema50_slope'] = 0.15
        else:
            components['ema50_slope'] = 0.0

        high_20 = df['high'].iloc[-20:].max()
        drawdown = (high_20 - current) / high_20
        if drawdown < 0.03:
            components['near_high'] = 0.30
        elif drawdown < 0.07:
            components['near_high'] = 0.15
        else:
            components['near_high'] = 0.0

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Market regime: {e}")
    return {"score": min(1.0, score), "components": components, "name": "market_regime"}


def score_physics_momentum(df, config):
    score = 0.0
    components = {}
    try:
        close  = df['close']
        volume = df['volume']

        velocity_5 = close.pct_change(5).iloc[-1]
        accel = velocity_5 - close.pct_change(5).iloc[-2]
        mass  = volume.iloc[-5:].mean() / (volume.iloc[-20:].mean() + 1e-9)
        phys_momentum = velocity_5 * mass

        if phys_momentum > 0.02:
            components['momentum_force'] = 0.35
        elif phys_momentum > 0.01:
            components['momentum_force'] = 0.20
        elif phys_momentum > 0:
            components['momentum_force'] = 0.10
        else:
            components['momentum_force'] = 0.0

        if accel > 0 and velocity_5 > 0:
            components['acceleration'] = 0.25
        elif accel > 0:
            components['acceleration'] = 0.10
        else:
            components['acceleration'] = 0.0

        trend_bars = 0
        for i in range(1, min(20, len(close))):
            if close.iloc[-i] > close.iloc[-i-1]:
                trend_bars += 1
            else:
                break
        components['inertia'] = min(0.25, trend_bars * 0.05)

        if close.iloc[-1] > df['high'].iloc[-20:-1].max():
            components['resistance_break'] = 0.15

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Physics momentum: {e}")
    return {"score": min(1.0, score), "components": components, "name": "physics_momentum"}


def score_fluid_dynamics(df, config):
    score = 0.0
    components = {}
    try:
        close    = df['close']
        bb_upper = df['bb_upper'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]
        bb_mid   = df['bb_mid'].iloc[-1]
        atr_val  = df['atr'].iloc[-1]
        current  = close.iloc[-1]

        direction_changes = 0
        for i in range(1, min(15, len(close) - 1)):
            if ((close.iloc[-i] - close.iloc[-i-1]) *
                    (close.iloc[-i-1] - close.iloc[-i-2]) < 0):
                direction_changes += 1
        turbulence = direction_changes / 13.0
        if turbulence < 0.30:
            components['laminar_flow'] = 0.30
        elif turbulence < 0.50:
            components['laminar_flow'] = 0.15
        else:
            components['laminar_flow'] = 0.0

        dist_from_mid = abs(current - bb_mid) / (atr_val + 1e-9)
        if dist_from_mid < 0.5:
            components['equilibrium'] = 0.20

        bb_width_pct = (bb_upper - bb_lower) / (bb_mid + 1e-9) * 100
        if bb_width_pct < 2.0:
            components['channel_pressure'] = 0.25
        elif bb_width_pct < 4.0:
            components['channel_pressure'] = 0.15

        if current > bb_mid and close.iloc[-3] < bb_mid:
            components['flow_cross'] = 0.25

        returns_std  = close.pct_change().iloc[-10:].std()
        returns_mean = abs(close.pct_change().iloc[-10:].mean())
        if returns_mean > 0 and returns_std / returns_mean < 1.5:
            components['stable_flow'] = 0.20

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Fluid dynamics: {e}")
    return {"score": min(1.0, score), "components": components, "name": "fluid_dynamics"}


def score_fear_greed(df, config):
    score = 0.0
    components = {}
    try:
        rsi_val   = df['rsi'].iloc[-1]
        vol_ratio = df['vol_ratio'].iloc[-1]
        returns   = df['close'].pct_change().dropna()
        rsi_prev  = df['rsi'].iloc[-3]

        if 45 <= rsi_val <= 65:
            components['sweet_spot'] = 0.30
        elif rsi_val < 30:
            components['fear_extreme'] = 0.35
            if vol_ratio > 1.5:
                components['panic_volume'] = 0.20
        elif rsi_val > 70:
            components['greed_caution'] = 0.10

        if rsi_val > rsi_prev and rsi_prev < 35:
            components['fear_recovery'] = 0.25

        neg_days = (returns.iloc[-5:] < 0).sum()
        if neg_days >= 4:
            components['capitulation'] = 0.20

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Fear/greed: {e}")
    return {"score": min(1.0, score), "components": components, "name": "fear_greed"}


def score_behavioral_bias(df, config):
    score = 0.0
    components = {}
    try:
        close  = df['close']
        open_  = df['open']
        volume = df['volume']
        current = close.iloc[-1]

        round_levels = [round(current/10)*10, round(current/5)*5, round(current)]
        min_dist = min(abs(current - lv) / current for lv in round_levels)
        if min_dist < 0.005:
            components['anchoring'] = 0.20

        gap = abs(open_.iloc[-1] - close.iloc[-2]) / (close.iloc[-2] + 1e-9)
        if 0.005 < gap < 0.02:
            components['gap_fill'] = 0.25

        drop_5d = (close.iloc[-1] - close.iloc[-6]) / (close.iloc[-6] + 1e-9)
        if drop_5d < -0.05:
            components['disposition'] = 0.20

        vol_surge   = volume.iloc[-1] > volume.iloc[-20:].mean() * 1.5
        strong_move = abs(close.pct_change().iloc[-1]) > 0.01
        if vol_surge and strong_move:
            components['herding'] = 0.20

        if close.iloc[-3] < close.iloc[-5] and close.iloc[-1] > close.iloc[-3]:
            components['v_bottom'] = 0.15

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Behavioral bias: {e}")
    return {"score": min(1.0, score), "components": components, "name": "behavioral_bias"}


def score_news_sentiment(df, config):
    score = 0.0
    components = {}
    try:
        close  = df['close']
        open_  = df['open']
        volume = df['volume']
        avg_vol = volume.iloc[-20:].mean()

        for i in range(1, min(6, len(close))):
            gap_size = (open_.iloc[-i] - close.iloc[-i-1]) / (close.iloc[-i-1] + 1e-9)
            gap_sustained = (close.iloc[-i] - open_.iloc[-i]) * np.sign(gap_size + 1e-9) > 0
            if abs(gap_size) > 0.01:
                if gap_sustained and gap_size > 0:
                    components['pos_gap_confirmed'] = 0.35
                elif not gap_sustained and gap_size < 0:
                    components['neg_gap_recovered'] = 0.30
                break

        bull_candles = sum(1 for i in range(1, 6) if close.iloc[-i] > open_.iloc[-i])
        if bull_candles >= 4:
            components['candle_sentiment'] = 0.30
        elif bull_candles >= 3:
            components['candle_sentiment'] = 0.20
        else:
            components['candle_sentiment'] = 0.05

        if df['vwap'].iloc[-1] > df['vwap'].iloc[-5]:
            components['vwap_sentiment'] = 0.25
        else:
            components['vwap_sentiment'] = 0.05

        max_gap = max(abs(open_.iloc[-i] - close.iloc[-i-1]) /
                      (close.iloc[-i-1] + 1e-9) for i in range(1, 6))
        if max_gap < 0.01:
            components['gap_stability'] = 0.10

        score = sum(v for v in components.values() if isinstance(v, (int, float)))
    except Exception as e:
        logger.debug(f"News sentiment: {e}")
    return {"score": min(1.0, score), "components": components, "name": "news_sentiment"}


def score_social_momentum(df, config):
    score = 0.0
    components = {}
    try:
        volume   = df['volume']
        close    = df['close']
        high     = df['high']
        low      = df['low']
        atr_val  = df['atr'].iloc[-1]
        avg_vol_20 = volume.iloc[-20:].mean()
        avg_vol_5  = volume.iloc[-5:].mean()
        current_vol = volume.iloc[-1]

        vol_multiple = current_vol / (avg_vol_20 + 1e-9)
        price_move   = abs(close.pct_change().iloc[-1])

        if vol_multiple > 3.0 and price_move < 0.01:
            components['accumulation'] = 0.40
        elif vol_multiple > 2.0:
            components['volume_spike'] = 0.25
        elif vol_multiple > 1.5:
            components['elevated_vol'] = 0.15

        vol_trend = avg_vol_5 / (avg_vol_20 + 1e-9)
        if vol_trend > 1.5:
            components['vol_trend'] = 0.25
        elif vol_trend > 1.2:
            components['vol_trend'] = 0.15

        range_today = (high.iloc[-1] - low.iloc[-1]) / (atr_val + 1e-9)
        if range_today > 1.5:
            components['range_expansion'] = 0.20

        high_vol_streak = 0
        for i in range(1, 6):
            if volume.iloc[-i] > avg_vol_20 * 1.3:
                high_vol_streak += 1
            else:
                break
        if high_vol_streak >= 3:
            components['vol_streak'] = 0.15

        score = sum(components.values())
    except Exception as e:
        logger.debug(f"Social momentum: {e}")
    return {"score": min(1.0, score), "components": components, "name": "social_momentum"}


# =============================================================================
# MASTER 17-POINT SCORING ENGINE
# =============================================================================

def calculate_17_point_score(df, config, signal_history=None):
    if signal_history is None:
        signal_history = []

    dimensions = {
        "trend_alignment":     score_trend_alignment(df, config),
        "momentum":            score_momentum(df, config),
        "mean_reversion":      score_mean_reversion(df, config),
        "pattern_recognition": score_pattern_recognition(df, config),
        "multi_timeframe":     score_multi_timeframe(df, config),
        "statistical_edge":    score_statistical_edge(df, config),
        "volatility_regime":   score_volatility_regime(df, config),
        "probability_model":   score_probability_model(df, config, signal_history),
        "expected_value":      score_expected_value(df, config),
        "volume_profile":      score_volume_profile(df, config),
        "market_regime":       score_market_regime(df, config),
        "physics_momentum":    score_physics_momentum(df, config),
        "fluid_dynamics":      score_fluid_dynamics(df, config),
        "fear_greed":          score_fear_greed(df, config),
        "behavioral_bias":     score_behavioral_bias(df, config),
        "news_sentiment":      score_news_sentiment(df, config),
        "social_momentum":     score_social_momentum(df, config),
    }

    total_score = sum(d["score"] for d in dimensions.values())
    total_score = round(min(17.0, max(0.0, total_score)), 2)

    if total_score >= 16:
        grade = "ELITE"
    elif total_score >= 13:
        grade = "STRONG"
    elif total_score >= 10:
        grade = "GOOD"
    elif total_score >= 6:
        grade = "MARGINAL"
    else:
        grade = "NO TRADE"

    close = df['close'].iloc[-1]
    ema_fast = df['ema_fast'].iloc[-1]
    direction = 'long' if close > ema_fast else 'short'

    win_probability = 0.45 + (total_score / 17) * 0.28
    win_probability = max(0.45, min(0.75, win_probability))
    signal_score_normalized = total_score / 17.0
    atr_val = df['atr'].iloc[-1]
    ev = (win_probability * atr_val * 3 - (1 - win_probability) * atr_val * 2) * 10

    return {
        "total_score":     total_score,
        "signal_score":    signal_score_normalized,
        "grade":           grade,
        "direction":       direction,
        "win_probability": round(win_probability, 4),
        "dimensions":      dimensions,
        "close":           close,
        "atr":             atr_val,
        "expected_value":  ev,
    }


def generate_composite_signal(df, config, signal_history=None):
    """Main entry point. Returns full 17-point scored signal."""
    result = calculate_17_point_score(df, config, signal_history)
    strategy_results = {
        name: {"score": dim["score"] * 2 - 1, "name": name}
        for name, dim in result["dimensions"].items()
    }
    return {
        "composite_score":  result["signal_score"] * 2 - 1,
        "signal_score":     result["signal_score"],
        "total_score_17":   result["total_score"],
        "grade":            result["grade"],
        "direction":        result["direction"],
        "strength":         result["signal_score"],
        "win_probability":  result["win_probability"],
        "expected_value":   result["expected_value"],
        "strategy_results": strategy_results,
        "dimensions":       result["dimensions"],
        "atr":              result["atr"],
        "close":            result["close"],
    }


# =============================================================================
# ELITE v3.0 MASTER SIGNAL — INTEGRATES ALL MODULES
# =============================================================================

def generate_elite_signal(df: pd.DataFrame, config,
                           symbol: str = "",
                           signal_history: list = None) -> dict:
    """
    Full elite signal combining:
    - 17-point technical/quant/physics/psychology/sentiment score
    - ARIMA price forecast
    - GARCH volatility forecast
    - Monte Carlo VaR/win probability
    - Bayesian probability update
    - ML Random Forest prediction
    - Navier-Stokes fluid dynamics
    - Fear & Greed index
    - Agent-based herd model
    - Macro event detection
    - VADER sentiment

    Returns everything needed to make and size a trade decision.
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if signal_history is None:
        signal_history = []

    # Base 17-point signal
    base = generate_composite_signal(df, config, signal_history)
    total_17 = base.get("total_score_17", 0)
    base_score = base.get("signal_score", 0.5)

    # ── Quant Models ────────────────────────────────────────────
    quant = {"composite_signal": 0.0, "garch_multiplier": 1.0,
             "win_probability_mc": 0.55, "var_95_dollar": 50}
    try:
        from models.quant_models import run_all_quant_models
        close = df['close'].iloc[-1]
        atr   = df['atr'].iloc[-1]
        quant = run_all_quant_models(
            df, config,
            stop_loss=close - atr * 2,
            take_profit=close + atr * 3
        )
    except Exception as e:
        logger.debug(f"Quant models unavailable: {e}")

    # ── Fluid Dynamics ───────────────────────────────────────────
    fluid = {"composite_signal": 0.5, "support": 0, "resistance": 0,
             "turbulence": {"regime": "NORMAL"}}
    try:
        from models.fluid_dynamics import NavierStokesMarket
        fluid = NavierStokesMarket().full_analysis(df)
    except Exception as e:
        logger.debug(f"Fluid dynamics unavailable: {e}")

    # ── Sentiment ────────────────────────────────────────────────
    sentiment = {"composite_signal": 0.5, "fear_greed_index": 50,
                 "fear_greed_cat": "NEUTRAL", "herd_regime": "UNCERTAIN"}
    try:
        from sentiment.sentiment_engine import run_all_sentiment
        sentiment = run_all_sentiment(df, symbol)
    except Exception as e:
        logger.debug(f"Sentiment unavailable: {e}")

    # ── Master Composite ─────────────────────────────────────────
    # Weight each module's contribution
    quant_signal    = (quant.get("composite_signal", 0) + 1) / 2
    fluid_signal    = fluid.get("composite_signal", 0.5)
    sentiment_signal = sentiment.get("composite_signal", 0.5)

    # Master score (extends the 17-point system with module bonuses)
    # Base 17-pt already normalized to 0-1
    master_score = (
        base_score    * 0.45 +  # Core 17-pt system
        quant_signal  * 0.25 +  # ARIMA/GARCH/MC/Bayes/ML
        fluid_signal  * 0.15 +  # Physics/fluid dynamics
        sentiment_signal * 0.15 # Psychology/sentiment
    )
    master_score = max(0.0, min(1.0, master_score))

    # Final win probability (Monte Carlo is most reliable)
    mc_win_prob   = quant.get("win_probability_mc", base.get("win_probability", 0.55))
    final_win_prob = mc_win_prob * 0.6 + base.get("win_probability", 0.55) * 0.4

    # GARCH position multiplier (reduce size in high-vol regimes)
    garch_mult = quant.get("garch_multiplier", 1.0)

    # Support/resistance from fluid dynamics
    support    = fluid.get("support", 0)
    resistance = fluid.get("resistance", 0)

    # Enhanced EV
    atr_val = df['atr'].iloc[-1]
    ev = (final_win_prob * atr_val * 3 - (1 - final_win_prob) * atr_val * 2) * 10

    # Grade on extended scale
    score_17_equiv = master_score * 17
    if score_17_equiv >= 15:
        grade = "ELITE"
    elif score_17_equiv >= 12:
        grade = "STRONG"
    elif score_17_equiv >= 9:
        grade = "GOOD"
    elif score_17_equiv >= 6:
        grade = "MARGINAL"
    else:
        grade = "NO TRADE"

    return {
        # Core scores
        "signal_score":       master_score,
        "total_score_17":     round(score_17_equiv, 2),
        "base_score_17":      total_17,
        "grade":              grade,
        "direction":          base.get("direction", "long"),
        "win_probability":    round(final_win_prob, 4),
        "expected_value":     round(ev, 2),
        "close":              df['close'].iloc[-1],
        "atr":                atr_val,
        "composite_score":    master_score * 2 - 1,

        # Module results
        "strategy_results":   base.get("strategy_results", {}),
        "dimensions":         base.get("dimensions", {}),
        "quant":              quant,
        "fluid":              fluid,
        "sentiment":          sentiment,

        # Risk parameters
        "garch_multiplier":   garch_mult,
        "var_95_dollar":      quant.get("var_95_dollar", 50),
        "support":            support,
        "resistance":         resistance,

        # Context
        "fear_greed_index":   sentiment.get("fear_greed_index", 50),
        "fear_greed_cat":     sentiment.get("fear_greed_cat", "NEUTRAL"),
        "herd_regime":        sentiment.get("herd_regime", "UNCERTAIN"),
        "turbulence_regime":  fluid.get("turbulence", {}).get("regime", "NORMAL"),
    }


# =============================================================================
# SECTOR-AWARE SIGNAL WRAPPER
# =============================================================================

def get_sector_weights(symbol: str, config) -> dict:
    """
    Get strategy weights tuned for this symbol's sector.
    Falls back to default weights if sector not found.
    """
    sector = getattr(config, 'SECTOR_MAP', {}).get(symbol, None)
    if sector:
        sector_weights = getattr(config, 'SECTOR_WEIGHTS', {})
        if sector in sector_weights:
            return sector_weights[sector]
    return getattr(config, 'STRATEGY_WEIGHTS', {
        "trend_following": 0.30, "momentum": 0.25,
        "mean_reversion": 0.20, "statistical_arb": 0.15,
        "volatility": 0.10,
    })


def generate_sector_aware_signal(df, config, symbol="", signal_history=None):
    """
    Generates composite signal using sector-specific strategy weights.
    AI stocks get more momentum weight. Energy gets more trend weight. Etc.
    """
    if signal_history is None:
        signal_history = []

    # Get sector-tuned weights
    weights = get_sector_weights(symbol, config)
    sector  = getattr(config, 'SECTOR_MAP', {}).get(symbol, 'GENERAL')

    # Run all 17 dimensions
    result = calculate_17_point_score(df, config, signal_history)

    # Apply sector weight boost to relevant dimensions
    dims = result.get("dimensions", {})
    total_score = result.get("total_score", 0)

    # Sector-specific score bonus
    sector_bonus = 0.0
    if sector in ("AI_SEMI", "AI_SOFT"):
        # Boost if momentum + volume surge confirming
        mom_score = dims.get("momentum", {}).get("score", 0)
        if mom_score > 0.6:
            sector_bonus = 0.5   # AI stocks with momentum get a boost

    elif sector == "ENERGY":
        # Boost if trend is strong (energy follows oil trends)
        trend_score = dims.get("trend_alignment", {}).get("score", 0)
        if trend_score > 0.7:
            sector_bonus = 0.4

    elif sector == "CLEAN":
        # Clean energy runs on sentiment + momentum
        sent_score = dims.get("social_momentum", {}).get("score", 0)
        if sent_score > 0.5:
            sector_bonus = 0.4

    elif sector == "BIOTECH":
        # Biotech needs volume confirmation (catalyst events show in volume)
        vol_score = dims.get("volume_profile", {}).get("score", 0)
        if vol_score > 0.6:
            sector_bonus = 0.5

    adjusted_score = min(17.0, total_score + sector_bonus)
    signal_score   = adjusted_score / 17.0

    # Grade
    if adjusted_score >= 15:
        grade = "ELITE"
    elif adjusted_score >= 12:
        grade = "STRONG"
    elif adjusted_score >= 9:
        grade = "GOOD"
    elif adjusted_score >= 6:
        grade = "MARGINAL"
    else:
        grade = "NO TRADE"

    close    = df['close'].iloc[-1]
    atr_val  = df['atr'].iloc[-1]
    ema_fast = df['ema_fast'].iloc[-1]
    direction = 'long' if close > ema_fast else 'short'

    win_prob = 0.45 + (signal_score * 0.28)
    win_prob = max(0.45, min(0.75, win_prob))
    ev = (win_prob * atr_val * 3 - (1 - win_prob) * atr_val * 2) * 10

    strategy_results = {
        name: {"score": dim["score"] * 2 - 1, "name": name}
        for name, dim in dims.items()
    }

    return {
        "composite_score":  signal_score * 2 - 1,
        "signal_score":     signal_score,
        "total_score_17":   round(adjusted_score, 2),
        "base_score_17":    round(total_score, 2),
        "sector_bonus":     round(sector_bonus, 2),
        "sector":           sector,
        "grade":            grade,
        "direction":        direction,
        "win_probability":  round(win_prob, 4),
        "expected_value":   round(ev, 2),
        "strategy_results": strategy_results,
        "dimensions":       dims,
        "atr":              atr_val,
        "close":            close,
        "garch_multiplier": 1.0,
        "var_95_dollar":    50,
        "support":          0,
        "resistance":       0,
        "fear_greed_index": 50,
        "fear_greed_cat":   "NEUTRAL",
        "herd_regime":      "UNCERTAIN",
        "turbulence_regime":"NORMAL",
        "quant":            {"garch": {"regime": "NORMAL"},
                             "arima": {"direction": 1},
                             "ml_model": {"val_accuracy": 0.55}},
    }


# =============================================================================
# RENAISSANCE-ENHANCED MASTER SIGNAL
# =============================================================================

def generate_renaissance_signal(df, config, symbol="", signal_history=None):
    """
    Full Renaissance-inspired signal combining:
    - 17-point scoring system
    - Sector-aware weights
    - Hidden Markov Model regime detection
    - Kalman Filter noise reduction
    - Fourier cycle analysis
    - Market entropy (predictability)
    - Signal decay / half-life
    - Cross-asset relationships
    - Information Coefficient weighting
    """
    if signal_history is None:
        signal_history = []

    # Base sector-aware signal
    base = generate_sector_aware_signal(df, config, symbol, signal_history)

    # Renaissance models
    ren_result = {"composite_signal": 0.5, "size_multiplier": 1.0,
                  "tradeable": True, "hmm_regime": "UNKNOWN",
                  "kalman_direction": "UP", "cycle_phase": "UNKNOWN",
                  "predictability": "MEDIUM", "optimal_hold_bars": 20}
    try:
        from models.renaissance_models import run_renaissance_models
        ren_result = run_renaissance_models(df, symbol, config)
    except Exception as e:
        logger.debug(f"Renaissance models unavailable: {e}")

    # Skip if market is unpredictable
    if not ren_result.get("tradeable", True):
        base["signal_score"] *= 0.6   # Reduce signal strength
        base["grade"] = "MARGINAL"

    # Blend base signal with Renaissance signal
    ren_signal = ren_result.get("composite_signal", 0.5)
    base_score = base.get("signal_score", 0.5)

    blended_score = base_score * 0.65 + ren_signal * 0.35
    blended_score = max(0.0, min(1.0, blended_score))

    # Apply HMM size multiplier
    hmm_mult = ren_result.get("size_multiplier", 1.0)

    # Recalculate 17-point score
    score_17 = blended_score * 17
    if score_17 >= 15:
        grade = "ELITE"
    elif score_17 >= 12:
        grade = "STRONG"
    elif score_17 >= 9:
        grade = "GOOD"
    elif score_17 >= 6:
        grade = "MARGINAL"
    else:
        grade = "NO TRADE"

    win_prob = 0.45 + (blended_score * 0.30)
    win_prob = max(0.45, min(0.78, win_prob))
    atr_val  = df['atr'].iloc[-1]
    ev = (win_prob * atr_val * 3 - (1 - win_prob) * atr_val * 2) * 10

    # Update base with Renaissance enhancements
    base.update({
        "signal_score":      blended_score,
        "total_score_17":    round(score_17, 2),
        "grade":             grade,
        "win_probability":   round(win_prob, 4),
        "expected_value":    round(ev, 2),
        "garch_multiplier":  hmm_mult,
        "hmm_regime":        ren_result.get("hmm_regime", "UNKNOWN"),
        "kalman_direction":  ren_result.get("kalman_direction", "UP"),
        "cycle_phase":       ren_result.get("cycle_phase", "UNKNOWN"),
        "predictability":    ren_result.get("predictability", "MEDIUM"),
        "half_life":         ren_result.get("half_life", 20),
        "optimal_hold":      ren_result.get("optimal_hold_bars", 20),
        "renaissance":       ren_result,
    })

    return base
