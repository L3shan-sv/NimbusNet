"""
lstm.py — Temporal fault type prediction via LSTM sequence model.

The LSTM is the model that makes NimbusNet genuinely predictive rather
than merely reactive. It observes sequences of feature vectors over time
and learns to recognise the temporal signature of each fault type before
the fault fully manifests.

Key insight: Network failures have temporal fingerprints.
    - Latency spikes precede packet loss by ~200–500ms
    - SYN floods show exponential connect rate growth before impact
    - Region partitions show sudden RTT → infinity, not gradual degradation
    - Cascading failures show multi-fault sequences with specific ordering

The LSTM learns these sequences from tc qdisc chaos sessions (Phase 6)
which generate exactly the temporal patterns we need as training data.

Architecture:
    Input:  sequence of (seq_len, 18) feature vectors
    LSTM:   2 layers, 64 hidden units each, dropout 0.2
    Output: softmax over 7 FaultType classes

Training:
    Bootstrap: synthetic tc qdisc sessions (1000+ sequences per fault type)
    Flywheel: every production failover adds a new labeled sequence
    GameDay: complex multi-fault sequences that synthetic data can't cover
"""

from __future__ import annotations

import logging
import time
import pickle
from pathlib import Path
from typing import Optional, Deque
from collections import deque

import numpy as np

from .types import FeatureVector, FaultType

logger = logging.getLogger(__name__)


# ─── Pure-NumPy LSTM for inference (no framework dependency at runtime) ────────
# Full PyTorch training code is in ml/pipeline/lstm_trainer.py.
# At inference time we load pre-trained weights and run forward pass manually.
# This avoids a 2GB PyTorch dependency in the production agent.

class LSTMCell:
    """Single LSTM cell — manual NumPy implementation for inference only."""

    def __init__(self, W: np.ndarray, U: np.ndarray, b: np.ndarray):
        # W: (4*hidden, input), U: (4*hidden, hidden), b: (4*hidden,)
        self.W = W
        self.U = U
        self.b = b
        self.hidden_size = b.shape[0] // 4

    def forward(
        self,
        x: np.ndarray,     # (input_size,)
        h: np.ndarray,     # (hidden_size,)
        c: np.ndarray,     # (hidden_size,)
    ) -> tuple[np.ndarray, np.ndarray]:
        gates = self.W @ x + self.U @ h + self.b
        i = _sigmoid(gates[0 * self.hidden_size: 1 * self.hidden_size])   # input gate
        f = _sigmoid(gates[1 * self.hidden_size: 2 * self.hidden_size])   # forget gate
        g = np.tanh(gates[2 * self.hidden_size:   3 * self.hidden_size])  # cell gate
        o = _sigmoid(gates[3 * self.hidden_size: 4 * self.hidden_size])   # output gate
        c_new = f * c + i * g
        h_new = o * np.tanh(c_new)
        return h_new, c_new


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -88, 88)))


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()


class NimbusLSTM:
    """
    Two-layer LSTM for fault type classification.

    Maintains a sliding window of the last `seq_len` feature vectors.
    Produces a FaultType prediction on every new feature vector.

    Lifecycle:
        1. load()       — load pre-trained weights from disk
        2. push()       — add new FeatureVector to sliding window
        3. predict()    — run forward pass on current window
        4. reset()      — clear window (e.g. after confirmed fault resolution)
    """

    VERSION = "1.0.0"

    def __init__(self, seq_len: int = 20, hidden_size: int = 64, n_classes: int = 7):
        """
        Args:
            seq_len:     Number of 50ms windows in the input sequence (= 1 second of history)
            hidden_size: LSTM hidden dimension
            n_classes:   Number of FaultType values (7)
        """
        self.seq_len     = seq_len
        self.hidden_size = hidden_size
        self.n_classes   = n_classes
        self.n_features  = FeatureVector.N_FEATURES  # 18

        # Weights (set by load())
        self._lstm1: Optional[LSTMCell] = None
        self._lstm2: Optional[LSTMCell] = None
        self._W_out: Optional[np.ndarray] = None   # (n_classes, hidden_size)
        self._b_out: Optional[np.ndarray] = None   # (n_classes,)

        # Sliding window of feature vectors
        self._window: Deque[np.ndarray] = deque(maxlen=seq_len)

        # Version tracking
        self._trained_at: Optional[float] = None
        self._training_sequences: int = 0
        self._version_hash: str = ""

    # ─── Inference ────────────────────────────────────────────────────────────

    def push(self, fv: FeatureVector) -> None:
        """Add a new feature vector to the sliding window."""
        self._window.append(fv.values.copy())

    def predict(self) -> tuple[FaultType, float, np.ndarray]:
        """
        Run forward pass on the current window.

        Returns:
            fault_type:   Most likely FaultType
            confidence:   Softmax probability of predicted class
            probabilities: Full softmax distribution over all 7 FaultTypes
        """
        if self._lstm1 is None:
            raise RuntimeError("LSTM weights not loaded. Call load() first.")

        if len(self._window) < 2:
            # Not enough history — default to UNKNOWN
            probs = np.zeros(self.n_classes, dtype=np.float32)
            probs[FaultType.UNKNOWN] = 1.0
            return FaultType.UNKNOWN, 1.0, probs

        # Pad sequence to seq_len with zeros if needed
        seq = list(self._window)
        if len(seq) < self.seq_len:
            padding = [np.zeros(self.n_features, dtype=np.float32)] * (self.seq_len - len(seq))
            seq = padding + seq
        seq_array = np.stack(seq)  # (seq_len, n_features)

        # Forward pass through two LSTM layers
        h1 = np.zeros(self.hidden_size, dtype=np.float32)
        c1 = np.zeros(self.hidden_size, dtype=np.float32)
        h2 = np.zeros(self.hidden_size, dtype=np.float32)
        c2 = np.zeros(self.hidden_size, dtype=np.float32)

        for t in range(self.seq_len):
            h1, c1 = self._lstm1.forward(seq_array[t], h1, c1)
            h2, c2 = self._lstm2.forward(h1, h2, c2)

        # Output layer (uses final hidden state h2)
        logits = self._W_out @ h2 + self._b_out
        probs  = _softmax(logits).astype(np.float32)

        predicted_idx  = int(np.argmax(probs))
        confidence     = float(probs[predicted_idx])
        fault_type     = FaultType(predicted_idx)

        return fault_type, confidence, probs

    def reset(self) -> None:
        """Clear the sliding window. Call after fault resolution."""
        self._window.clear()

    # ─── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """Save weights to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version":             self.VERSION,
            "seq_len":             self.seq_len,
            "hidden_size":         self.hidden_size,
            "n_classes":           self.n_classes,
            "lstm1_W":             self._lstm1.W if self._lstm1 else None,
            "lstm1_U":             self._lstm1.U if self._lstm1 else None,
            "lstm1_b":             self._lstm1.b if self._lstm1 else None,
            "lstm2_W":             self._lstm2.W if self._lstm2 else None,
            "lstm2_U":             self._lstm2.U if self._lstm2 else None,
            "lstm2_b":             self._lstm2.b if self._lstm2 else None,
            "W_out":               self._W_out,
            "b_out":               self._b_out,
            "trained_at":          self._trained_at,
            "training_sequences":  self._training_sequences,
            "version_hash":        self._version_hash,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("LSTM saved", extra={"path": str(path)})

    @classmethod
    def load(cls, path: str | Path) -> "NimbusLSTM":
        """Load weights from disk."""
        with open(path, "rb") as f:
            payload = pickle.load(f)

        inst = cls(
            seq_len=payload["seq_len"],
            hidden_size=payload["hidden_size"],
            n_classes=payload["n_classes"],
        )

        if payload["lstm1_W"] is not None:
            inst._lstm1 = LSTMCell(payload["lstm1_W"], payload["lstm1_U"], payload["lstm1_b"])
            inst._lstm2 = LSTMCell(payload["lstm2_W"], payload["lstm2_U"], payload["lstm2_b"])
            inst._W_out = payload["W_out"]
            inst._b_out = payload["b_out"]

        inst._trained_at          = payload.get("trained_at")
        inst._training_sequences  = payload.get("training_sequences", 0)
        inst._version_hash        = payload.get("version_hash", "")

        logger.info(
            "LSTM loaded",
            extra={
                "path": str(path),
                "seq_len": inst.seq_len,
                "sequences": inst._training_sequences,
            }
        )
        return inst

    @classmethod
    def from_random_weights(cls, seq_len: int = 20, hidden_size: int = 64, n_classes: int = 7) -> "NimbusLSTM":
        """
        Create an untrained model with random weights for testing.
        Do NOT use in production — call load() with trained weights.
        """
        rng = np.random.default_rng(42)
        n_features = FeatureVector.N_FEATURES

        def rand_lstm_weights(in_size: int, h_size: int):
            scale = np.sqrt(2.0 / (in_size + h_size))
            W = rng.normal(0, scale, (4 * h_size, in_size)).astype(np.float32)
            U = rng.normal(0, scale, (4 * h_size, h_size)).astype(np.float32)
            b = np.zeros(4 * h_size, dtype=np.float32)
            return W, U, b

        inst = cls(seq_len=seq_len, hidden_size=hidden_size, n_classes=n_classes)
        W1, U1, b1 = rand_lstm_weights(n_features, hidden_size)
        W2, U2, b2 = rand_lstm_weights(hidden_size, hidden_size)
        inst._lstm1 = LSTMCell(W1, U1, b1)
        inst._lstm2 = LSTMCell(W2, U2, b2)

        scale_out = np.sqrt(2.0 / (hidden_size + n_classes))
        inst._W_out = rng.normal(0, scale_out, (n_classes, hidden_size)).astype(np.float32)
        inst._b_out = np.zeros(n_classes, dtype=np.float32)

        return inst

    @property
    def is_loaded(self) -> bool:
        return self._lstm1 is not None

    @property
    def window_fill_ratio(self) -> float:
        """How full the sliding window is (0.0 – 1.0)."""
        return len(self._window) / self.seq_len

    @property
    def metadata(self) -> dict:
        return {
            "version":            self.VERSION,
            "version_hash":       self._version_hash,
            "trained_at":         self._trained_at,
            "training_sequences": self._training_sequences,
            "seq_len":            self.seq_len,
            "hidden_size":        self.hidden_size,
        }
