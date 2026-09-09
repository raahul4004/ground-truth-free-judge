"""Experiment 5 -- does the policy layer help on a family it has never seen?

Experiment 4 produced an uncomfortable result: zeroing the policy features
*improves* in-mix dev balanced accuracy and MCC, while worsening ECE and the
confidence AUROC. Taken alone that argues for deleting them.

But in-mix cross-validation is the wrong test for these features. Their whole
purpose is to survive a change in the family mix, and dev's mix is not the mix
they will be judged on -- held-out moves `hardship_request` from 3.3% to 18.3%
and `autopay_cancel` from 10.8% to 3.3%, and the private mix is unpublished.
A feature that memorises "how often does this family pass" scores well when
train and test share a mix and collapses when they do not.

So: leave-one-family-out. Train with a family entirely absent, then judge only
that family. This is a direct measurement of the situation the private set puts
the judge in, and it is the test that should decide whether the policy layer
stays.

Run:  python experiments/05_transfer.py
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

ARMS: dict[str, tuple[str, ...]] = {
    "full system": (),
    "without policy features": ("pol_",),
    "without escalation-required only": (
        "pol_escalation_required", "pol_required_and_", "pol_not_required",
        "pol_n_escalation_reasons", "pol_required_escalated_cleanly",
    ),
    "without family one-hots": ("fam_",),
    "without policy AND family": ("pol_", "fam_"),
}


def fit_predict(Xtr, ytr, gtr, Xte, gate_te):
    oof = np.zeros(len(ytr))
    models = []
    n = min(N_FOLDS, len(set(gtr)))
    for itr, ite in GroupKFold(n_splits=n).split(Xtr, ytr, gtr):
        m = _hgb().fit(Xtr[itr], ytr[itr])
        oof[ite] = m.predict_proba(Xtr[ite])[:, 1]
        models.append(m)
    p = np.clip(oof, 1e-6, 1 - 1e-6)
    cal = LogisticRegression(C=1e6, max_iter=1000).fit(
        np.log(p / (1 - p)).reshape(-1, 1), ytr
    )
    raw = np.column_stack([m.predict_proba(Xte)[:, 1] for m in models]).mean(axis=1)
    pr = np.clip(raw, 1e-6, 1 - 1e-6)
    prob = cal.predict_proba(np.log(pr / (1 - pr)).reshape(-1, 1))[:, 1]
    return np.where(gate_te, GATE_PROBABILITY, prob)


def main() -> None:
    schemas = load_tool_schemas(os.path.join(DATA, "tool_schemas.json"))
    with open(os.path.join(DATA, "dev.jsonl"), encoding="utf-8") as h:
        dev = [json.loads(line) for line in h if line.strip()]
    matrix, names = vectorise(dev, schemas)
    X0 = np.asarray(matrix, dtype=float)
    y = np.asarray([int(r["label"]) for r in dev], dtype=int)
    groups = np.asarray([r["trajectory"]["case"]["case_id"] for r in dev])
    fams = np.asarray([r["trajectory"]["case"]["family"] for r in dev])
    gates = load_gates(os.path.join(ROOT, "experiments", "gates.json"))
    idx = {n: i for i, n in enumerate(names)}

    gate_mask = np.zeros(len(y), dtype=bool)
    for g in gates:
        gate_mask |= X0[:, idx[g]] > 0

    results: dict[str, dict] = {}
    per_family: dict[str, dict[str, float]] = {}

    for arm, prefixes in ARMS.items():
        X = X0.copy()
        if prefixes:
            cols = [i for n, i in idx.items() if n.startswith(prefixes)]
            X[:, cols] = 0.0
        prob = np.zeros(len(y))
        for fam in sorted(set(fams)):
            te = fams == fam
            tr = ~te
            prob[te] = fit_predict(X[tr], y[tr], groups[tr], X[te], gate_mask[te])
        pred = (prob >= THRESHOLD).astype(int)
        rep = metrics.report(y.tolist(), pred.tolist(), confidence_from_probability(prob).tolist())
        results[arm] = rep
        per_family[arm] = {
            fam: float((pred[fams == fam] == y[fams == fam]).mean())
            for fam in sorted(set(fams))
        }

    print("Leave-one-family-out: every row judged by a model that never saw its family\n")
    print(f"{'arm':<36} {'BA':>7} {'MCC':>7} {'ECE':>7} {'AUROC':>7}  {'bars':>9}")
    print("-" * 80)
    for arm, rep in results.items():
        status = metrics.bar_status(rep)
        ok = "all pass" if all(v for v in status.values() if v is not None) else "FAILS"
        print(f"{arm:<36} {rep['balanced_accuracy']:>7.4f} {rep['mcc']:>7.4f} "
              f"{rep['ece']:>7.4f} {rep['confidence_auroc']:>7.4f}  {ok:>9}")

    base = results["full system"]
    print("\ndelta vs full system (LOFO):")
    for arm, rep in results.items():
        if arm == "full system":
            continue
        print(f"  {arm:<36} BA {rep['balanced_accuracy'] - base['balanced_accuracy']:+.4f}   "
              f"MCC {rep['mcc'] - base['mcc']:+.4f}   "
              f"AUROC {rep['confidence_auroc'] - base['confidence_auroc']:+.4f}")

    print("\nper-family accuracy, full vs without policy features:")
    full_f, nop_f = per_family["full system"], per_family["without policy features"]
    print(f"  {'family':<22} {'full':>7} {'no-pol':>7} {'delta':>8}")
    for fam in sorted(full_f):
        d = full_f[fam] - nop_f[fam]
        print(f"  {fam:<22} {full_f[fam]:>7.3f} {nop_f[fam]:>7.3f} {d:>+8.3f}")

    out = os.path.join(ROOT, "results", "transfer_lofo.json")
    with open(out, "w", encoding="utf-8") as h:
        json.dump({"threshold": THRESHOLD, "arms": results, "per_family": per_family}, h, indent=2)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
