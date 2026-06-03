"""
=============================================================================
FLUID DYNAMICS & PHYSICS ENGINE v3.0
=============================================================================
Applies physics principles to market microstructure:

NAVIER-STOKES ANALOGY:
  - Price = fluid velocity field
  - Volume = fluid density/mass
  - Momentum = rho * v (density * velocity)
  - Viscosity = market friction (bid-ask spread, slippage)
  - Turbulence = volatility regimes
  - Reynolds number = trend vs chaotic regime detector
  - Bernoulli effect = price acceleration at breakouts (narrow channel)
  - Vorticity = circular price patterns (consolidation)
  - Pressure = order book imbalance

THERMODYNAMICS:
  - Entropy = market disorder/uncertainty
  - Temperature = volatility level
  - Phase transitions = trend reversals
=============================================================================
"""

import numpy as np
import pandas as pd
import logging
from scipy import stats

logger = logging.getLogger(__name__)


class NavierStokesMarket:
    """
    Navier-Stokes inspired market model.
    Models price movement as fluid flow through a channel.
    """

    def calculate_reynolds_number(self, df: pd.DataFrame) -> dict:
        """
        Reynolds Number analogy for markets.
        
        In fluid dynamics: Re = (rho * v * L) / mu
        In markets:
          rho (density)   = volume / average volume
          v (velocity)    = price rate of change
          L (length scale)= trend duration in bars
          mu (viscosity)  = bid-ask spread proxy (ATR%)

        Re < 2300 = LAMINAR flow (steady trend, predictable)
        Re > 4000 = TURBULENT flow (choppy, unpredictable)
        2300-4000 = TRANSITIONAL (changing regime)
        """
        try:
            close    = df['close']
            volume   = df['volume']
            atr_pct  = df['atr_pct'].iloc[-1]

            # Fluid analogs
            rho = volume.iloc[-5:].mean() / (volume.iloc[-20:].mean() + 1e-9)
            v   = abs(close.pct_change(5).iloc[-1]) * 100
            L   = self._trend_duration(close)
            mu  = max(0.01, atr_pct)  # Viscosity = friction

            # Reynolds number (scaled to market context)
            Re = (rho * v * L) / mu
            Re_scaled = Re * 100  # Scale to 0-10000 range

            # Regime classification
            if Re_scaled < 2300:
                regime = "LAMINAR"
                regime_score = 0.8   # Predictable — trade with trend
                description = "Smooth trending flow"
            elif Re_scaled < 4000:
                regime = "TRANSITIONAL"
                regime_score = 0.5   # Uncertain — reduce size
                description = "Regime change possible"
            else:
                regime = "TURBULENT"
                regime_score = 0.2   # Chaotic — avoid or hedge
                description = "High turbulence — unpredictable"

            return {
                "reynolds_number": round(Re_scaled, 1),
                "regime":          regime,
                "regime_score":    regime_score,
                "description":     description,
                "rho":             round(rho, 3),
                "velocity":        round(v, 4),
                "viscosity":       round(mu, 4),
                "L":               L,
            }

        except Exception as e:
            logger.debug(f"Reynolds number error: {e}")
            return {"regime": "UNKNOWN", "regime_score": 0.5,
                    "reynolds_number": 0}

    def _trend_duration(self, close: pd.Series) -> int:
        """Count consecutive bars in same direction."""
        n = 0
        direction = np.sign(close.iloc[-1] - close.iloc[-2])
        for i in range(1, min(20, len(close) - 1)):
            if np.sign(close.iloc[-i] - close.iloc[-i-1]) == direction:
                n += 1
            else:
                break
        return max(1, n)

    def calculate_momentum_flux(self, df: pd.DataFrame) -> dict:
        """
        Momentum flux = rho * v^2 (from Navier-Stokes momentum equation).
        High flux = strong directional force behind the move.
        Low flux = move lacks conviction.
        """
        try:
            close  = df['close']
            volume = df['volume']
            atr    = df['atr'].iloc[-1]

            # Density (normalized volume)
            rho = volume.iloc[-5:].mean() / (volume.iloc[-20:].mean() + 1e-9)

            # Velocity (price rate of change)
            v = close.pct_change(5).iloc[-1]

            # Momentum flux (F = rho * v^2, but keep sign of v)
            flux = rho * v * abs(v)

            # Normalize
            flux_norm = np.tanh(flux * 20)  # squash to -1,+1

            # Convective acceleration (d(velocity)/dt)
            v_current = close.pct_change(5).iloc[-1]
            v_prev    = close.pct_change(5).iloc[-2]
            accel     = v_current - v_prev

            return {
                "momentum_flux":    round(float(flux_norm), 4),
                "velocity":         round(float(v), 4),
                "density":          round(float(rho), 3),
                "acceleration":     round(float(accel), 4),
                "signal":           round(float(flux_norm), 4),
            }

        except Exception as e:
            logger.debug(f"Momentum flux error: {e}")
            return {"momentum_flux": 0.0, "signal": 0.0}

    def calculate_pressure_zones(self, df: pd.DataFrame) -> dict:
        """
        Pressure zones = support/resistance as fluid pressure walls.
        
        High-volume price levels create pressure zones where price
        stalls or bounces — like fluid hitting a wall.
        
        Bernoulli principle: pressure drops as velocity increases.
        When price breaks through a narrow zone, it accelerates.
        """
        try:
            close  = df['close']
            high   = df['high']
            low    = df['low']
            volume = df['volume']
            atr    = df['atr'].iloc[-1]
            current = close.iloc[-1]

            # Volume-weighted price levels (high volume = strong wall)
            prices = close.iloc[-50:].values
            vols   = volume.iloc[-50:].values

            # Find pressure zones using volume-weighted histogram
            price_min = prices.min()
            price_max = prices.max()
            n_bins = 20
            bin_edges = np.linspace(price_min, price_max, n_bins + 1)
            bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

            pressure = np.zeros(n_bins)
            for p, v in zip(prices, vols):
                bin_idx = min(n_bins - 1,
                              int((p - price_min) / (price_max - price_min + 1e-9) * n_bins))
                pressure[bin_idx] += v

            # Find nearest high-pressure zones above and below current price
            current_bin = int((current - price_min) / (price_max - price_min + 1e-9) * n_bins)
            current_bin = min(n_bins - 1, max(0, current_bin))

            # Support = highest pressure zone below current price
            below = [(bin_centers[i], pressure[i])
                     for i in range(current_bin) if bin_centers[i] < current]
            support = max(below, key=lambda x: x[1])[0] if below else current - atr * 2

            # Resistance = highest pressure zone above current price
            above = [(bin_centers[i], pressure[i])
                     for i in range(current_bin + 1, n_bins) if bin_centers[i] > current]
            resistance = max(above, key=lambda x: x[1])[0] if above else current + atr * 2

            # Distance to nearest pressure zone
            dist_support    = (current - support) / atr
            dist_resistance = (resistance - current) / atr

            # Bernoulli score: if we're far from walls, momentum carries through
            # If near a wall, expect slowdown/reversal
            if dist_support < 0.5:
                pressure_signal = 0.8   # Near support — bounce likely
            elif dist_resistance < 0.5:
                pressure_signal = -0.3  # Near resistance — slowdown likely
            else:
                pressure_signal = 0.4   # Open field — momentum carries

            return {
                "support":          round(float(support), 4),
                "resistance":       round(float(resistance), 4),
                "dist_to_support":  round(float(dist_support), 2),
                "dist_to_resist":   round(float(dist_resistance), 2),
                "pressure_signal":  round(float(pressure_signal), 4),
                "signal":           round(float(pressure_signal), 4),
            }

        except Exception as e:
            logger.debug(f"Pressure zones error: {e}")
            return {"support": 0, "resistance": 0, "signal": 0.0}

    def calculate_turbulence_index(self, df: pd.DataFrame) -> dict:
        """
        Market turbulence index.
        Based on Mahalanobis distance of returns from historical mean.
        High turbulence = avoid or reduce position size.
        """
        try:
            returns = df['close'].pct_change().dropna()
            if len(returns) < 30:
                return {"turbulence": 0.5, "signal": 0.3}

            recent = returns.iloc[-5:].values
            hist   = returns.iloc[-60:-5].values

            hist_mean = hist.mean()
            hist_std  = hist.std()

            # Z-score of recent returns vs history
            z_scores = (recent - hist_mean) / (hist_std + 1e-9)
            turbulence = float(np.mean(np.abs(z_scores)))

            # Shannon entropy of returns (higher = more disorder)
            hist_vals, _ = np.histogram(returns.iloc[-30:], bins=10,
                                         density=True)
            hist_vals = hist_vals + 1e-9
            entropy = -np.sum(hist_vals * np.log(hist_vals))
            entropy_norm = entropy / np.log(10)  # Normalize by max entropy

            if turbulence < 1.0:
                signal = 0.7    # Low turbulence — trade with confidence
            elif turbulence < 2.0:
                signal = 0.4    # Moderate — caution
            else:
                signal = 0.1    # High turbulence — avoid

            return {
                "turbulence_index": round(turbulence, 3),
                "entropy":          round(float(entropy_norm), 3),
                "signal":           signal,
                "regime": "CALM" if turbulence < 1 else
                           "VOLATILE" if turbulence < 2 else "CRISIS"
            }

        except Exception as e:
            logger.debug(f"Turbulence error: {e}")
            return {"turbulence_index": 1.0, "signal": 0.4}

    def calculate_vorticity(self, df: pd.DataFrame) -> dict:
        """
        Vorticity = circular/rotational motion in price.
        High vorticity = price spinning (consolidation, coiling).
        Low vorticity after high = breakout from consolidation.
        """
        try:
            close = df['close']
            returns = close.pct_change()

            # Vorticity proxy: autocorrelation of returns
            # Positive autocorr = trending (laminar)
            # Negative autocorr = mean-reverting (vortex)
            r = returns.dropna().iloc[-20:].values
            if len(r) < 4:
                return {"vorticity": 0, "signal": 0.5}

            autocorr_1 = np.corrcoef(r[:-1], r[1:])[0, 1]
            autocorr_2 = np.corrcoef(r[:-2], r[2:])[0, 1]

            # Vorticity = tendency to reverse direction
            vorticity = -autocorr_1  # Negative autocorr = high vorticity

            # Vorticity DECREASING (coming out of consolidation) = breakout signal
            r_old = returns.dropna().iloc[-40:-20].values
            if len(r_old) >= 4:
                old_autocorr = np.corrcoef(r_old[:-1], r_old[1:])[0, 1]
                vorticity_trend = autocorr_1 - old_autocorr
            else:
                vorticity_trend = 0

            # Signal: low vorticity (trending) is good for trend trades
            signal = 0.3 + (autocorr_1 * 0.4)
            signal = max(0.0, min(1.0, signal))

            return {
                "vorticity":       round(float(vorticity), 4),
                "autocorr_lag1":   round(float(autocorr_1), 4),
                "autocorr_lag2":   round(float(autocorr_2), 4),
                "vorticity_trend": round(float(vorticity_trend), 4),
                "signal":          round(float(signal), 4),
            }

        except Exception as e:
            logger.debug(f"Vorticity error: {e}")
            return {"vorticity": 0, "signal": 0.5}

    def full_analysis(self, df: pd.DataFrame) -> dict:
        """Run complete fluid dynamics analysis."""
        reynolds    = self.calculate_reynolds_number(df)
        flux        = self.calculate_momentum_flux(df)
        pressure    = self.calculate_pressure_zones(df)
        turbulence  = self.calculate_turbulence_index(df)
        vorticity   = self.calculate_vorticity(df)

        # Composite fluid signal
        composite = (
            reynolds.get("regime_score", 0.5) * 0.25 +
            (flux.get("signal", 0) * 0.5 + 0.5) * 0.25 +
            pressure.get("signal", 0.5) * 0.20 +
            turbulence.get("signal", 0.5) * 0.20 +
            vorticity.get("signal", 0.5) * 0.10
        )

        return {
            "composite_signal": round(float(composite), 4),
            "reynolds":         reynolds,
            "momentum_flux":    flux,
            "pressure_zones":   pressure,
            "turbulence":       turbulence,
            "vorticity":        vorticity,
            "support":          pressure.get("support", 0),
            "resistance":       pressure.get("resistance", 0),
        }
