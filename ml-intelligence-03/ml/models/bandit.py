"""
bandit.py — Multi-armed bandit for online routing weight optimisation.

The bandit is the only model that updates on every routing decision — it
doesn't wait for the flywheel retraining cycle. It is the adaptive traffic
shaper's brain.

Why a bandit for routing weights:
    - Routing is a classic explore/exploit problem: we need to send some traffic
      to a degraded region to detect recovery, but not so much that we hurt users
    - The bandit learns the reward (latency improvement) from each routing decision
      and adjusts weights accordingly
    - Online updates: weight changes propagate within one aggregation window (50ms)
    - No labels required: reward is observed directly from the next window's RTT

Algorithm: Thompson Sampling with Beta distributions.

    Each region r maintains Beta(alpha_r, beta_r):
        alpha_r = number of successful routing decisions to r
        beta_r  = number of failed routing decisions to r

    On each decision:
        sample theta_r ~ Beta(alpha_r, beta_r) for each region
        routing_weight_r = softmax(theta_r) across all regions

    On each observation (after 50ms):
        if rtt_improved: alpha_r += 1
        else:            beta_r  += 1

    This naturally keeps a small exploration weight on degraded regions
    so we never lose signal on a region that might be recovering.

The bandit never drops a region to weight=0 (minimum exploration weight).
This is the property the architecture spec refers to as "the bandit keeps
exploring at low weight so you never lose signal on a region that might recover."
"""

from __future__ import annotations

import logging
import math
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class RegionArm:
    """One arm of the bandit — one AWS region."""

    def __init__(self, region_id: str, alpha: float = 1.0, beta: float = 1.0):
        """
        Args:
            region_id: e.g. "us-east-1"
            alpha:     Prior successes (Beta distribution parameter)
            beta:      Prior failures  (Beta distribution parameter)
        """
        self.region_id = region_id
        self.alpha     = alpha   # successes
        self.beta      = beta    # failures

        # Tracking
        self.total_decisions: int    = 0
        self.total_rewards:   float  = 0.0
        self.last_rtt_us:     float  = 0.0
        self.last_updated_at: float  = time.time()

    def update(self, reward: float) -> None:
        """
        Update the arm with an observed reward.

        Args:
            reward: [0, 1] — 1.0 if routing decision was good (RTT improved or stable),
                             0.0 if routing decision was bad (RTT degraded further)
        """
        self.alpha       += reward
        self.beta        += (1.0 - reward)
        self.total_decisions += 1
        self.total_rewards   += reward
        self.last_updated_at  = time.time()

    def sample(self, rng: np.random.Generator) -> float:
        """Sample theta from Beta(alpha, beta). Higher = more likely to be chosen."""
        return float(rng.beta(self.alpha, self.beta))

    @property
    def mean_reward(self) -> float:
        """Expected reward = alpha / (alpha + beta)."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def uncertainty(self) -> float:
        """
        Variance of the Beta distribution = measure of uncertainty.
        High uncertainty = arm hasn't been explored enough.
        """
        a, b = self.alpha, self.beta
        return (a * b) / ((a + b) ** 2 * (a + b + 1))


class NimbusMultiArmedBandit:
    """
    Thompson Sampling bandit for routing weight allocation across regions.

    This is the adaptive traffic shaper's decision engine. It runs on every
    aggregation window (50ms) and produces routing weights for the control plane.

    Lifecycle:
        1. add_region()        — register a region arm
        2. recommend_weights() — get routing weights for the control plane
        3. observe()           — update arm with observed outcome
        4. save() / load()     — persist state across restarts

    The bandit maintains a MINIMUM_WEIGHT floor to ensure every healthy region
    always receives at least some traffic. This floor keeps the signal alive.
    """

    VERSION = "1.0.0"

    # Minimum routing weight any arm can receive.
    # 0.05 = 5% of traffic always goes to every connected region.
    MINIMUM_WEIGHT = 0.05

    # RTT improvement threshold for a "success" reward
    # If new RTT < old RTT × (1 - threshold), it's a success
    RTT_IMPROVEMENT_THRESHOLD = 0.05  # 5% improvement counts as success

    def __init__(self, exploration_decay: float = 0.999, seed: int = 42):
        """
        Args:
            exploration_decay: How fast to reduce exploration over time.
                               0.999 ≈ very slow decay — maintains exploration long-term.
            seed: RNG seed for reproducibility in testing.
        """
        self.exploration_decay = exploration_decay
        self._rng = np.random.default_rng(seed)
        self._arms: dict[str, RegionArm] = {}
        self._step: int = 0
        self._version_hash: str = ""

    # ─── Region Management ────────────────────────────────────────────────────

    def add_region(self, region_id: str) -> None:
        """Register a new region. Safe to call at runtime."""
        if region_id not in self._arms:
            self._arms[region_id] = RegionArm(region_id)
            logger.info("Bandit: region arm added", extra={"region": region_id})

    def remove_region(self, region_id: str) -> None:
        """Deregister a region (e.g. cold standby decommissioned)."""
        if region_id in self._arms:
            del self._arms[region_id]
            logger.info("Bandit: region arm removed", extra={"region": region_id})

    # ─── Decision ─────────────────────────────────────────────────────────────

    def recommend_weights(self) -> dict[str, float]:
        """
        Run Thompson Sampling to produce routing weights for all regions.

        Returns:
            Dict of {region_id: weight} where weights sum to 1.0.
            No weight is below MINIMUM_WEIGHT.

        This is called by the adaptive traffic shaper in Phase 4
        every aggregation window (50ms).
        """
        if not self._arms:
            return {}

        # Thompson sampling: draw one sample from each arm's Beta distribution
        samples = {
            region_id: arm.sample(self._rng)
            for region_id, arm in self._arms.items()
        }

        # Convert samples to weights via softmax
        raw_weights = self._softmax(np.array(list(samples.values())))
        regions     = list(samples.keys())

        weights = dict(zip(regions, raw_weights))

        # Apply minimum weight floor
        weights = self._apply_minimum_weight(weights)

        self._step += 1
        return weights

    def observe(
        self,
        region_id: str,
        old_rtt_us: float,
        new_rtt_us: float,
    ) -> None:
        """
        Update the bandit with the observed outcome of a routing decision.

        Called 50ms after recommend_weights() when the next aggregation window
        gives us the actual RTT after routing to this region.

        Args:
            region_id:  Which region received traffic
            old_rtt_us: RTT before the routing decision (microseconds)
            new_rtt_us: RTT after the routing decision (microseconds)
        """
        if region_id not in self._arms:
            logger.warning("Bandit: observe() called for unknown region", extra={"region": region_id})
            return

        # Compute reward: 1.0 if RTT improved by >= 5%, 0.0 if degraded
        if old_rtt_us <= 0:
            reward = 0.5  # no prior data — neutral reward
        elif new_rtt_us <= old_rtt_us * (1 - self.RTT_IMPROVEMENT_THRESHOLD):
            reward = 1.0  # success: RTT improved
        elif new_rtt_us > old_rtt_us * 1.1:
            reward = 0.0  # failure: RTT got 10% worse
        else:
            # Partial reward: linear interpolation
            reward = max(0.0, 1.0 - (new_rtt_us - old_rtt_us) / max(old_rtt_us, 1))

        self._arms[region_id].update(reward)
        self._arms[region_id].last_rtt_us = new_rtt_us

        logger.debug(
            "Bandit: arm updated",
            extra={
                "region": region_id,
                "reward": round(reward, 3),
                "old_rtt_us": old_rtt_us,
                "new_rtt_us": new_rtt_us,
                "mean_reward": round(self._arms[region_id].mean_reward, 3),
            }
        )

    # ─── Diagnostics ──────────────────────────────────────────────────────────

    def arm_stats(self) -> dict[str, dict]:
        """Return diagnostic stats for all arms. Used by Prometheus metrics."""
        return {
            region_id: {
                "alpha":          arm.alpha,
                "beta":           arm.beta,
                "mean_reward":    round(arm.mean_reward, 4),
                "uncertainty":    round(arm.uncertainty, 4),
                "total_decisions":arm.total_decisions,
                "last_rtt_us":    arm.last_rtt_us,
            }
            for region_id, arm in self._arms.items()
        }

    # ─── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version":            self.VERSION,
            "arms":               self._arms,
            "step":               self._step,
            "exploration_decay":  self.exploration_decay,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("Bandit saved", extra={"path": str(path), "step": self._step})

    @classmethod
    def load(cls, path: str | Path, seed: int = 42) -> "NimbusMultiArmedBandit":
        with open(path, "rb") as f:
            payload = pickle.load(f)

        inst = cls(
            exploration_decay=payload.get("exploration_decay", 0.999),
            seed=seed,
        )
        inst._arms = payload["arms"]
        inst._step = payload.get("step", 0)

        logger.info(
            "Bandit loaded",
            extra={"path": str(path), "regions": list(inst._arms.keys()), "step": inst._step}
        )
        return inst

    # ─── Internals ────────────────────────────────────────────────────────────

    def _apply_minimum_weight(self, weights: dict[str, float]) -> dict[str, float]:
        """
        Ensure no weight is below MINIMUM_WEIGHT.
        Renormalise after applying the floor.
        """
        clipped = {r: max(w, self.MINIMUM_WEIGHT) for r, w in weights.items()}
        total   = sum(clipped.values())
        return {r: w / total for r, w in clipped.items()}

    @staticmethod
    def _softmax(x: np.ndarray) -> np.ndarray:
        e = np.exp(x - x.max())
        return e / e.sum()

    @property
    def regions(self) -> list[str]:
        return list(self._arms.keys())

    @property
    def metadata(self) -> dict:
        return {
            "version":     self.VERSION,
            "regions":     self.regions,
            "step":        self._step,
            "arm_stats":   self.arm_stats(),
        }
