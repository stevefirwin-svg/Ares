"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  RAPTOR v11 — IC-WEIGHTED ENSEMBLE VOTER                                    ║
║                                                                              ║
║  Engine weights are no longer static guesses.                               ║
║  They are continuously updated based on each engine's Information           ║
║  Coefficient (IC) — the empirical correlation between predicted signal       ║
║  strength and realized return.                                               ║
║                                                                              ║
║  ICIR (IC Information Ratio) = IC_mean / IC_std                             ║
║  High ICIR = consistent edge. Low ICIR = noise. Negative IC = reverse it.   ║
║                                                                              ║
║  Reference: Grinold & Kahn (2000), Clarke, de Silva & Thorley (2002)       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import logging
import numpy as np
from config import ENGINE_WEIGHTS
from core.math_core import ICTracker, apply_decay_to_ensemble

logger = logging.getLogger("Raptor.Ensemble")


class EnsembleVoter:
    def __init__(self):
        self.ic_tracker = ICTracker(decay_halflife=20, min_samples=10)
        self._base_weights = dict(ENGINE_WEIGHTS)
        self._cycle_count  = 0

    def vote(self, engine_scores: dict, macro_regime: dict,
             signal_ages: dict = None, hurst_mults: dict = None
             ) -> tuple:
        """
        Full ensemble vote with:
        1. IC-weighted dynamic engine weights
        2. Regime-adaptive weight tilts
        3. Signal decay penalty
        4. Hurst-based weight modulation
        5. Multiplicative conviction score

        Returns: (ensemble_score, agreement_ratio, conviction_multiplier, weights_used)
        """
        self._cycle_count += 1

        # Step 1: IC-derived dynamic weights
        dynamic_weights = self.ic_tracker.get_dynamic_weights(self._base_weights)

        # Step 2: Regime tilt — Dalio-style conditional rebalancing
        regime  = macro_regime.get("regime", "GROWTH")
        weights = self._apply_regime_tilt(dynamic_weights, regime)

        # Step 3: Hurst modulation per engine
        if hurst_mults:
            weights = self._apply_hurst_modulation(weights, hurst_mults)

        # Step 4: Normalize
        total = sum(weights.values()) or 1e-10
        weights = {k: v / total for k, v in weights.items()}

        # Step 5: Signal decay
        scores = dict(engine_scores)
        if signal_ages:
            scores = apply_decay_to_ensemble(scores, signal_ages)

        # Step 6: Weighted ensemble score
        # E1 FIX: Don't default missing engines to 0.5 — they contribute no signal.
        # Only engines that actually produced a score participate in the vote.
        # Missing PEAD/ANALYST engines previously pulled ensemble toward 0.5 always.
        available_engines = {eng: w for eng, w in weights.items() if eng in scores}
        if not available_engines:
            available_engines = weights  # fallback if all missing

        # Renormalize weights to available engines only
        avail_total = sum(available_engines.values()) or 1e-10
        available_weights = {k: v / avail_total for k, v in available_engines.items()}

        ensemble_score = sum(
            scores.get(eng, 0.5) * w
            for eng, w in available_weights.items()
        )

        # Step 7: Agreement ratio — E2 FIX: use same (decayed) scores as vote
        n_engines  = len(available_weights)
        n_agree    = sum(1 for e in available_weights if scores.get(e, 0) > 0.55)
        agreement  = n_agree / max(1, n_engines)

        # Step 8: Conviction score using available engines
        conviction = 1.0
        n_positive = 0
        for engine, w in available_weights.items():
            score  = scores.get(engine, 0.5)
            margin = score - 0.50
            if margin > 0:
                conviction *= (1.0 + margin * w * 4.0)
                n_positive += 1
            else:
                conviction *= max(0.60, 1.0 + margin * w * 3.0)

        breadth_bonus = np.sqrt(max(1, n_positive)) / np.sqrt(max(1, n_engines))
        conviction    = float(conviction * breadth_bonus)
        conviction    = float(np.clip(conviction, 0.0, 4.0))

        logger.debug(f"Ensemble: score={ensemble_score:.4f} agree={agreement:.2f} "
                     f"conviction={conviction:.3f} regime={regime} weights={weights}")

        return float(ensemble_score), float(agreement), float(conviction), weights

    def _apply_regime_tilt(self, weights: dict, regime: str) -> dict:
        """Dalio-style regime conditional tilts."""
        w = dict(weights)
        if regime == "GROWTH":
            w["trend"]    = w.get("trend",    0.28) * 1.15
            w["momentum"] = w.get("momentum", 0.22) * 1.12
            w["flow"]     = w.get("flow",     0.22) * 1.08
        elif regime == "RECESSION":
            w["regime"]   = w.get("regime",   0.12) * 1.60
            w["flow"]     = w.get("flow",     0.22) * 1.25
            w["momentum"] = w.get("momentum", 0.22) * 0.65
            w["trend"]    = w.get("trend",    0.28) * 0.75
        elif regime == "INFLATION":
            w["flow"]     = w.get("flow",     0.22) * 1.30
            w["trend"]    = w.get("trend",    0.28) * 0.88
            w["regime"]   = w.get("regime",   0.12) * 1.20
        elif regime == "REFLATION":
            w["trend"]    = w.get("trend",    0.28) * 1.08
            w["momentum"] = w.get("momentum", 0.22) * 1.05
        return w

    def _apply_hurst_modulation(self, weights: dict, hurst_mults: dict) -> dict:
        """
        Hurst-based weight modulation:
        - Trending market: up-weight momentum/trend, down-weight mean-reversion signals
        - Mean-reverting market: suppress momentum, up-weight flow/regime
        """
        w = dict(weights)
        momentum_mult = hurst_mults.get("momentum_mult", 1.0)
        reversion_mult= hurst_mults.get("reversion_mult", 1.0)

        w["trend"]     = w.get("trend",    0.28) * momentum_mult
        w["momentum"]  = w.get("momentum", 0.22) * momentum_mult
        w["flow"]      = w.get("flow",     0.22) * reversion_mult
        w["vwap_ma200"]= w.get("vwap_ma200", 0.16) * (0.9 + 0.1 * reversion_mult)
        return w

    def record_outcome(self, engine_scores: dict, realized_return: float):
        """
        Called after a trade closes with the actual return.
        Updates IC tracking for all engines.
        """
        for engine, score in engine_scores.items():
            if engine in self._base_weights:
                self.ic_tracker.add_observation(engine, score, realized_return)

    def get_weights_display(self) -> dict:
        """Current live weights for dashboard display."""
        return self.ic_tracker.get_dynamic_weights(self._base_weights)

    def get_ic_display(self) -> dict:
        """IC summary for dashboard."""
        return self.ic_tracker.get_ic_summary()
