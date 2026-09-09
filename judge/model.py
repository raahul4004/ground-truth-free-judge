"""The structural judge: a calibrated probability of ``label == 1``.

Design, and why each piece is there:

* **Fold ensemble.** Five gradient-boosted models, each trained on a
  ``GroupKFold`` split over ``case_id``. Predictions average the five. Dev has
  only 120 distinct cases behind 480 rows, so a single fit on all of it is
  high-variance; averaging folds costs nothing at inference (no model calls)
  and the out-of-fold predictions come free as the calibration set.

* **Platt calibration, not isotonic.** The scorer's confidence AUROC counts
  ties as half a win, so isotonic regression -- piecewise constant, and
  therefore tie-generating -- actively damages the metric this design exists
  to win. A sigmoid fit is strictly monotone and keeps the ordering intact.

* **Gates override the model.** The checks in ``gates.json`` are exact on dev:
  they never fire on a run the database says passed. When one fires the
  probability is clamped near zero rather than left to the model, so a
  certainty stays a certainty.

Confidence reported to the scorer is ``max(p, 1 - p)`` -- confidence in the
verdict actually emitted, which is what its ECE and AUROC are computed over.
"""

from __future__ import annotations

import json
import os
import pickle
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

from .featurize import feature_names, vectorise

SEED = 20260915
N_FOLDS = 5

# Probability assigned when a deterministic gate fires. Not 0.0: the scorer
# rewards being right more than being loud, and a hard 0 would make a single
# gate bug unrecoverable. 0.02 keeps confidence at 0.98.
GATE_PROBABILITY = 0.02

DEFAULT_GATES = (
    'gate_no_commit',
    'has_empty_assistant_turn',
    'gate_inert_commit',
    'pol_m_mandatory_escalation_missed',
    'commit_claims_no_action',
    'pol_m_unverified_closed_without_escalation',
    'pol_v_refused_then_no_escalation',
    'pol_v_schedule_date_out_of_window',
)


def _hgb() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_depth=3,
        max_iter=300,
        learning_rate=0.06,
        l2_regularization=1.0,
        min_samples_leaf=15,
        random_state=SEED,
    )


class StructuralJudge:
    """Fold-ensembled, Platt-calibrated, gate-overridden verdict model."""

    def __init__(self, gates: tuple[str, ...] = DEFAULT_GATES, threshold: float = 0.5) -> None:
        self.gates = tuple(gates)
        self.threshold = float(threshold)
        self.models: list[HistGradientBoostingClassifier] = []
        self.calibrator: LogisticRegression | None = None
        self.names: tuple[str, ...] = ()
        self.oof: np.ndarray | None = None
        self.train_rows = 0

    # -- fitting ---------------------------------------------------------

    def fit(self, rows: list[dict], schemas: dict[str, dict]) -> "StructuralJudge":
        matrix, names = vectorise(rows, schemas)
        X = np.asarray(matrix, dtype=float)
        y = np.asarray([int(r["label"]) for r in rows], dtype=int)
        groups = np.asarray([r["trajectory"]["case"]["case_id"] for r in rows])
        self.names = names
        self.train_rows = len(rows)

        oof = np.zeros(len(y), dtype=float)
        self.models = []
        for train_idx, test_idx in GroupKFold(n_splits=N_FOLDS).split(X, y, groups):
            model = _hgb()
            model.fit(X[train_idx], y[train_idx])
            oof[test_idx] = model.predict_proba(X[test_idx])[:, 1]
            self.models.append(model)
        self.oof = oof

        # Platt scaling on the out-of-fold scores. Strictly monotone, so the
        # confidence ranking the AUROC bar measures survives.
        logit = np.clip(oof, 1e-6, 1 - 1e-6)
        z = np.log(logit / (1 - logit)).reshape(-1, 1)
        self.calibrator = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
        self.calibrator.fit(z, y)
        return self

    # -- inference -------------------------------------------------------

    def _gate_mask(self, X: np.ndarray) -> np.ndarray:
        idx = {name: i for i, name in enumerate(self.names)}
        mask = np.zeros(X.shape[0], dtype=bool)
        for gate in self.gates:
            if gate in idx:
                mask |= X[:, idx[gate]] > 0
            else:  # pragma: no cover - guards a renamed feature
                raise KeyError(f"gate feature missing from vector: {gate}")
        return mask

    def _raw_proba(self, X: np.ndarray) -> np.ndarray:
        preds = np.column_stack([m.predict_proba(X)[:, 1] for m in self.models])
        return preds.mean(axis=1)

    def _calibrate(self, prob: np.ndarray) -> np.ndarray:
        assert self.calibrator is not None
        p = np.clip(prob, 1e-6, 1 - 1e-6)
        z = np.log(p / (1 - p)).reshape(-1, 1)
        return self.calibrator.predict_proba(z)[:, 1]

    def probabilities(self, rows: list[dict], schemas: dict[str, dict]) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(calibrated_probability, gate_mask)``."""
        matrix, names = vectorise(rows, schemas)
        if names != self.names:
            raise ValueError("feature layout changed since training; retrain the model")
        X = np.asarray(matrix, dtype=float)
        prob = self._calibrate(self._raw_proba(X))
        gates = self._gate_mask(X)
        prob = np.where(gates, GATE_PROBABILITY, prob)
        return prob, gates

    def feature_matrix(self, rows: list[dict], schemas: dict[str, dict]) -> np.ndarray:
        matrix, _names = vectorise(rows, schemas)
        return np.asarray(matrix, dtype=float)

    # -- persistence -----------------------------------------------------

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            pickle.dump(
                {
                    "models": self.models,
                    "calibrator": self.calibrator,
                    "names": self.names,
                    "gates": self.gates,
                    "threshold": self.threshold,
                    "train_rows": self.train_rows,
                    "seed": SEED,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

    @classmethod
    def load(cls, path: str) -> "StructuralJudge":
        with open(path, "rb") as handle:
            blob: dict[str, Any] = pickle.load(handle)
        judge = cls(gates=tuple(blob["gates"]), threshold=float(blob["threshold"]))
        judge.models = blob["models"]
        judge.calibrator = blob["calibrator"]
        judge.names = tuple(blob["names"])
        judge.train_rows = int(blob.get("train_rows", 0))
        return judge


def confidence_from_probability(prob: np.ndarray) -> np.ndarray:
    """Confidence in the emitted verdict: ``max(p, 1 - p)``, clipped to [0, 1]."""
    return np.clip(np.maximum(prob, 1.0 - prob), 0.0, 1.0)


def load_gates(path: str) -> tuple[str, ...]:
    if not os.path.exists(path):
        return DEFAULT_GATES
    with open(path, encoding="utf-8") as handle:
        return tuple(json.load(handle))


__all__ = [
    "StructuralJudge",
    "confidence_from_probability",
    "feature_names",
    "load_gates",
    "DEFAULT_GATES",
    "GATE_PROBABILITY",
]
