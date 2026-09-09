"""Honest development-set evaluation via nested, grouped cross-validation.

Why nested. The model is a fold ensemble whose Platt calibrator is fitted on
out-of-fold scores. If we calibrated on all of dev and then reported metrics on
dev, the calibrator would have seen every row it is being scored on -- and ECE
and confidence AUROC are precisely the metrics a leaked calibrator flatters.
So each outer fold re-runs the whole procedure (fold models + calibrator +
gates) on its training cases only, and predicts cases it has never seen.

Grouping is by ``case_id`` everywhere, because four trajectories share each
case and dev/held-out are disjoint by case, not by row.

The numbers this prints are the ones claimed in submission.yaml. They are
recomputed by the organisers' own ``score.py``, which this script invokes.

Run:  python scripts/evaluate_dev.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from judge import metrics  # noqa: E402
from judge.featurize import vectorise  # noqa: E402
from judge.features import load_tool_schemas  # noqa: E402
from judge.model import (  # noqa: E402
    GATE_PROBABILITY,
    N_FOLDS,
    StructuralJudge,
    _hgb,
    confidence_from_probability,
    load_gates,
)
from judge.taxonomy import categories_for  # noqa: E402

# Fixture location. Honours the DATA environment variable so a reviewer can
# point at their own copy of references/OP-04/ without editing anything.
DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
RAW = os.path.join(RESULTS, "raw")
OUTER_FOLDS = 5

# Selected on dev by experiments/03_threshold.py. Kept as a module constant so
# the evaluation and the shipped model cannot silently disagree.
THRESHOLD = 0.525


def _fit_inner(X, y, groups, gate_idx):
    """Train fold models + calibrator on a training subset only."""
    oof = np.zeros(len(y), dtype=float)
    models = []
    n_splits = min(N_FOLDS, len(set(groups)))
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        m = _hgb()
        m.fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])[:, 1]
        models.append(m)
    p = np.clip(oof, 1e-6, 1 - 1e-6)
    z = np.log(p / (1 - p)).reshape(-1, 1)
    cal = LogisticRegression(C=1e6, max_iter=1000).fit(z, y)
    return models, cal


def _predict(models, cal, X, gate_idx):
    raw = np.column_stack([m.predict_proba(X)[:, 1] for m in models]).mean(axis=1)
    p = np.clip(raw, 1e-6, 1 - 1e-6)
    z = np.log(p / (1 - p)).reshape(-1, 1)
    prob = cal.predict_proba(z)[:, 1]
    gate = np.zeros(X.shape[0], dtype=bool)
    for i in gate_idx:
        gate |= X[:, i] > 0
    return np.where(gate, GATE_PROBABILITY, prob), gate


def main() -> None:
    os.makedirs(RAW, exist_ok=True)
    schemas = load_tool_schemas(os.path.join(DATA, "tool_schemas.json"))
    with open(os.path.join(DATA, "dev.jsonl"), encoding="utf-8") as h:
        dev = [json.loads(line) for line in h if line.strip()]

    matrix, names = vectorise(dev, schemas)
    X = np.asarray(matrix, dtype=float)
    y = np.asarray([int(r["label"]) for r in dev], dtype=int)
    groups = np.asarray([r["trajectory"]["case"]["case_id"] for r in dev])
    gates = load_gates(os.path.join(ROOT, "experiments", "gates.json"))
    name_idx = {n: i for i, n in enumerate(names)}
    gate_idx = [name_idx[g] for g in gates]

    print(f"dev rows={len(dev)} cases={len(set(groups))} features={len(names)}")
    print(f"gates ({len(gates)}): {', '.join(gates)}\n")

    prob = np.zeros(len(y), dtype=float)
    gate_mask = np.zeros(len(y), dtype=bool)
    for k, (tr, te) in enumerate(GroupKFold(n_splits=OUTER_FOLDS).split(X, y, groups), 1):
        models, cal = _fit_inner(X[tr], y[tr], groups[tr], gate_idx)
        prob[te], gate_mask[te] = _predict(models, cal, X[te], gate_idx)
        print(f"  outer fold {k}: trained on {len(tr)} rows, predicted {len(te)}")

    conf = confidence_from_probability(prob)
    pred = (prob >= THRESHOLD).astype(int)

    # Gate sanity: a gate firing on a passing run is a defect, not noise.
    if gate_mask.any():
        leak = y[gate_mask].sum()
        print(
            f"\ngates fired on {int(gate_mask.sum())} rows; passing runs among them: {int(leak)}"
            f" (must be 0)"
        )

    pred_path = os.path.join(RAW, "dev_oof_predictions.jsonl")
    gold_path = os.path.join(RAW, "dev_labels.jsonl")
    with open(pred_path, "w", encoding="utf-8") as ph, open(gold_path, "w", encoding="utf-8") as gh:
        for i, row in enumerate(dev):
            cats = categories_for(row, int(pred[i]))
            ph.write(json.dumps({
                "id": row["id"],
                "verdict": int(pred[i]),
                "confidence": round(float(conf[i]), 4),
                "category": cats[0],
            }) + "\n")
            gh.write(json.dumps({"id": row["id"], "label": int(y[i])}) + "\n")

    fams = [r["trajectory"]["case"]["family"] for r in dev]
    rep = metrics.report(y.tolist(), pred.tolist(), conf.tolist())
    print("\n" + metrics.format_report(rep, "nested grouped OOF on dev (honest estimate)"))

    # Recompute with the organisers' scorer -- the only numbers that count.
    print("\n--- organisers' score.py on the same files ---")
    out = subprocess.run(
        [sys.executable, os.path.join(DATA, "score.py"),
         "--pred", pred_path, "--gold", gold_path,
         "--report", os.path.join(RESULTS, "dev_score_report.json")],
        capture_output=True, text=True, check=False,
    )
    print(out.stdout.strip())
    if out.stderr.strip():
        print("stderr:", out.stderr.strip())
    if out.returncode != 0:
        sys.exit(f"score.py failed with code {out.returncode}")

    official = json.loads(open(os.path.join(RESULTS, "dev_score_report.json"), encoding="utf-8").read())
    mismatch = [
        k for k in ("balanced_accuracy", "mcc", "ece", "confidence_auroc")
        if official.get(k) != rep.get(k)
    ]
    print(f"\nparity with score.py: {'OK' if not mismatch else 'MISMATCH in ' + ', '.join(mismatch)}")

    # Per-family breakdown, so a family that only works on the dev mix is visible.
    print("\nper-family (nested OOF):")
    for fam in sorted(set(fams)):
        idx = [i for i, f in enumerate(fams) if f == fam]
        acc = float((pred[idx] == y[idx]).mean())
        print(f"   {fam:<22} n={len(idx):<3} gold_pass={y[idx].mean():.3f} accuracy={acc:.3f}")

    with open(os.path.join(RESULTS, "dev_oof_metrics.json"), "w", encoding="utf-8") as h:
        json.dump({"nested_grouped_oof": rep, "official_score_py": official,
                   "gates": list(gates), "outer_folds": OUTER_FOLDS,
                   "threshold": THRESHOLD}, h, indent=2)

    # Raw probabilities, so the threshold sweep runs on the same scores without
    # refitting anything.
    with open(os.path.join(RAW, "dev_oof_probabilities.json"), "w", encoding="utf-8") as h:
        json.dump(
            {
                "id": [r["id"] for r in dev],
                "probability": [float(p) for p in prob],
                "label": [int(v) for v in y],
                "gate_fired": [bool(g) for g in gate_mask],
                "family": fams,
            },
            h,
        )
    print(f"\nwrote {pred_path}")


if __name__ == "__main__":
    main()
