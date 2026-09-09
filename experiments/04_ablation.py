"""Experiment 4 -- per-lever ablation of the deterministic layers.

A single stacked before/after says nothing about which piece is load-bearing,
so each layer is removed on its own and the honest nested-CV dev estimate is
recomputed. Layers are removed by zeroing their feature columns, which keeps
the vector shape and the training procedure identical across arms.

This is also the record of two rejected ideas. The audit suggested that "the
customer declines to verify" and "identity was attempted twice and never
established" should count as mandatory-escalation reasons. Both are defensible
readings of the policy. Both measured *worse* on dev (-0.005 BA, -0.006 MCC),
so both were rejected and the flags kept as ordinary features. The
`vulnerable` and `debt_denied` pattern extensions measured exactly neutral and
were retained on policy grounds alone -- recorded here as unvalidated rather
than presented as a gain.

Run:  python experiments/04_ablation.py
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from judge import metrics  # noqa: E402
from judge.featurize import vectorise  # noqa: E402
from judge.features import load_tool_schemas  # noqa: E402
from judge.model import GATE_PROBABILITY, N_FOLDS, _hgb, confidence_from_probability, load_gates  # noqa: E402

# Fixture location. Honours the DATA environment variable so a reviewer can
# point at their own copy of references/OP-04/ without editing anything.
DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
THRESHOLD = 0.525
OUTER_FOLDS = 5

# Feature-name prefixes that make up each removable layer.
LAYERS: dict[str, tuple[str, ...]] = {
    "policy checks (pol_*)": ("pol_",),
    "escalation-required quadrants": (
        "pol_escalation_required", "pol_required_and_", "pol_not_required",
        "pol_n_escalation_reasons", "pol_required_escalated_cleanly",
    ),
    "refusal signals": ("n_refusals", "has_refusal", "refused_", "n_distinct_refused",
                        "refusal_", "all_refusals_recovered", "n_refusals_recovered",
                        "attempted_but_refused"),
    "intent + planted-span": ("intent_", "planted_", "n_extra_state_tools",
                              "any_extra_state_tool", "served_customer_",
                              "n_intents_in_message"),
    "family one-hots": ("fam_",),
    "loan facts": ("loan_",),
    "message lexical": ("msg_",),
    "commit-argument features": ("commit_",),
}


def run(X: np.ndarray, y: np.ndarray, groups: np.ndarray, gate_idx: list[int],
        zero_cols: list[int], use_gates: bool = True) -> dict:
    Xa = X.copy()
    if zero_cols:
        Xa[:, zero_cols] = 0.0
    prob = np.zeros(len(y))
    gate = np.zeros(len(y), dtype=bool)
    for tr, te in GroupKFold(n_splits=OUTER_FOLDS).split(Xa, y, groups):
        oof = np.zeros(len(tr))
        models = []
        for itr, ite in GroupKFold(n_splits=N_FOLDS).split(Xa[tr], y[tr], groups[tr]):
            m = _hgb().fit(Xa[tr][itr], y[tr][itr])
            oof[ite] = m.predict_proba(Xa[tr][ite])[:, 1]
            models.append(m)
        p = np.clip(oof, 1e-6, 1 - 1e-6)
        cal = LogisticRegression(C=1e6, max_iter=1000).fit(
            np.log(p / (1 - p)).reshape(-1, 1), y[tr]
        )
        raw = np.column_stack([m.predict_proba(Xa[te])[:, 1] for m in models]).mean(axis=1)
        pr = np.clip(raw, 1e-6, 1 - 1e-6)
        prob[te] = cal.predict_proba(np.log(pr / (1 - pr)).reshape(-1, 1))[:, 1]
        if use_gates:
            g = np.zeros(len(te), dtype=bool)
            for i in gate_idx:
                g |= X[te][:, i] > 0          # gates read the UNablated matrix
            gate[te] = g
            prob[te] = np.where(g, GATE_PROBABILITY, prob[te])
    pred = (prob >= THRESHOLD).astype(int)
    return metrics.report(y.tolist(), pred.tolist(), confidence_from_probability(prob).tolist())


def main() -> None:
    schemas = load_tool_schemas(os.path.join(DATA, "tool_schemas.json"))
    with open(os.path.join(DATA, "dev.jsonl"), encoding="utf-8") as h:
        dev = [json.loads(line) for line in h if line.strip()]
    matrix, names = vectorise(dev, schemas)
    X = np.asarray(matrix, dtype=float)
    y = np.asarray([int(r["label"]) for r in dev], dtype=int)
    groups = np.asarray([r["trajectory"]["case"]["case_id"] for r in dev])
    gates = load_gates(os.path.join(ROOT, "experiments", "gates.json"))
    idx = {n: i for i, n in enumerate(names)}
    gate_idx = [idx[g] for g in gates]

    full = run(X, y, groups, gate_idx, [])
    print(f"{'arm':<34} {'BA':>7} {'MCC':>7} {'ECE':>7} {'AUROC':>7}   delta BA / MCC")
    print("-" * 82)
    print(f"{'full system':<34} {full['balanced_accuracy']:>7.4f} {full['mcc']:>7.4f} "
          f"{full['ece']:>7.4f} {full['confidence_auroc']:>7.4f}")

    rows = [{"arm": "full system", **full}]

    gates_off = run(X, y, groups, gate_idx, [], use_gates=False)
    rows.append({"arm": "gates removed", **gates_off})
    print(f"{'  - gates':<34} {gates_off['balanced_accuracy']:>7.4f} {gates_off['mcc']:>7.4f} "
          f"{gates_off['ece']:>7.4f} {gates_off['confidence_auroc']:>7.4f}   "
          f"{gates_off['balanced_accuracy'] - full['balanced_accuracy']:+.4f} / "
          f"{gates_off['mcc'] - full['mcc']:+.4f}")

    for layer, prefixes in LAYERS.items():
        cols = [i for n, i in idx.items() if n.startswith(prefixes)]
        if not cols:
            continue
        rep = run(X, y, groups, gate_idx, cols)
        rows.append({"arm": f"- {layer}", "n_features_zeroed": len(cols), **rep})
        print(f"{'  - ' + layer:<34} {rep['balanced_accuracy']:>7.4f} {rep['mcc']:>7.4f} "
              f"{rep['ece']:>7.4f} {rep['confidence_auroc']:>7.4f}   "
              f"{rep['balanced_accuracy'] - full['balanced_accuracy']:+.4f} / "
              f"{rep['mcc'] - full['mcc']:+.4f}   ({len(cols)} cols)")

    out = os.path.join(ROOT, "results", "ablation.json")
    with open(out, "w", encoding="utf-8") as h:
        json.dump({"threshold": THRESHOLD, "arms": rows}, h, indent=2)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
