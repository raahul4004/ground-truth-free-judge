"""Experiment 1 -- how far do deterministic transcript features alone get?

Grouped by ``case_id`` throughout: dev and held-out are disjoint *by case*, so
a random row split would leak (four trajectories share each case) and flatter
the result. Leave-one-family-out is reported separately because the held-out
family mix differs sharply from dev.

Run:  python experiments/01_structural.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from judge import metrics  # noqa: E402
from judge.featurize import vectorise  # noqa: E402
from judge.features import load_tool_schemas  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Fixture location. Honours the DATA environment variable so a reviewer can
# point at their own copy of references/OP-04/ without editing anything.
DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
SEED = 20260915


def load(split: str) -> list[dict]:
    with open(os.path.join(DATA, f"{split}.jsonl"), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def case_of(row: dict) -> str:
    return row["trajectory"]["case"]["case_id"]


def to_conf(prob: np.ndarray) -> np.ndarray:
    """Confidence in the *predicted* class -- what the scorer's ECE/AUROC use."""
    return np.maximum(prob, 1.0 - prob)


def evaluate(y: np.ndarray, prob: np.ndarray, threshold: float = 0.5) -> dict:
    pred = (prob >= threshold).astype(int)
    return metrics.report(y.tolist(), pred.tolist(), to_conf(prob).tolist())


def grouped_oof(model_fn, X: np.ndarray, y: np.ndarray, groups: np.ndarray, folds: int = 5) -> np.ndarray:
    """Out-of-fold probabilities under GroupKFold on case_id."""
    oof = np.zeros(len(y), dtype=float)
    splitter = GroupKFold(n_splits=folds)
    for train_idx, test_idx in splitter.split(X, y, groups):
        model = model_fn()
        model.fit(X[train_idx], y[train_idx])
        oof[test_idx] = model.predict_proba(X[test_idx])[:, 1]
    return oof


def main() -> None:
    schemas = load_tool_schemas(os.path.join(DATA, "tool_schemas.json"))
    dev = load("dev")
    matrix, names = vectorise(dev, schemas)
    X = np.asarray(matrix, dtype=float)
    y = np.asarray([r["label"] for r in dev], dtype=int)
    groups = np.asarray([case_of(r) for r in dev])

    print(f"dev rows={len(dev)}  features={len(names)}  distinct cases={len(set(groups))}")
    print(f"base pass rate={y.mean():.4f}\n")

    # ---------------------------------------------------------------
    # Baseline A: the two deterministic gates only.
    # gate trips -> fail, otherwise -> pass. No model, no tokens.
    # ---------------------------------------------------------------
    gi = {n: i for i, n in enumerate(names)}
    with open(os.path.join(ROOT, "experiments", "gates.json"), encoding="utf-8") as h:
        gate_names = json.load(h)
    gate = np.zeros(len(dev), dtype=bool)
    for g in gate_names:
        gate |= X[:, gi[g]] > 0
    print(f"gates in use ({len(gate_names)}): {', '.join(gate_names)}\n")
    pred_gate = (~gate).astype(int)
    conf_gate = np.where(gate, 0.95, 0.70)
    rep = metrics.report(y.tolist(), pred_gate.tolist(), conf_gate.tolist())
    print(metrics.format_report(rep, "A) deterministic gates only (0 tokens)"))
    print(f"   gate trips on {gate.sum()} rows; of those, pass rate = {y[gate].mean():.4f}")
    print(f"   (a gate that ever fires on a passing run is a bug -- want 0.0000)\n")

    # ---------------------------------------------------------------
    # Baseline B: structural model, grouped out-of-fold.
    # ---------------------------------------------------------------
    models = {
        "logreg(C=0.5)": lambda: make_pipeline(
            StandardScaler(), LogisticRegression(C=0.5, max_iter=2000, random_state=SEED)
        ),
        "logreg(C=0.1)": lambda: make_pipeline(
            StandardScaler(), LogisticRegression(C=0.1, max_iter=2000, random_state=SEED)
        ),
        "hgb(depth=3)": lambda: HistGradientBoostingClassifier(
            max_depth=3, max_iter=300, learning_rate=0.06,
            l2_regularization=1.0, min_samples_leaf=15, random_state=SEED,
        ),
        "hgb(depth=2)": lambda: HistGradientBoostingClassifier(
            max_depth=2, max_iter=400, learning_rate=0.06,
            l2_regularization=1.0, min_samples_leaf=20, random_state=SEED,
        ),
    }

    oofs = {}
    for name, fn in models.items():
        oof = grouped_oof(fn, X, y, groups)
        oofs[name] = oof
        print(metrics.format_report(evaluate(y, oof), f"B) {name}  [grouped OOF]"))
        print()

    # ---------------------------------------------------------------
    # Baseline C: gates hard-override the model.
    # ---------------------------------------------------------------
    best_name = max(oofs, key=lambda k: evaluate(y, oofs[k])["balanced_accuracy"])
    oof = oofs[best_name].copy()
    oof_gated = np.where(gate, 0.02, oof)
    print(metrics.format_report(evaluate(y, oof_gated), f"C) {best_name} + gate override"))
    print()

    # ---------------------------------------------------------------
    # Threshold sweep on the gated model.
    # ---------------------------------------------------------------
    print("D) threshold sweep on C (balanced accuracy / mcc)")
    for t in np.arange(0.30, 0.71, 0.05):
        rep = evaluate(y, oof_gated, threshold=float(t))
        print(
            f"   t={t:.2f}  ba={rep['balanced_accuracy']:.4f}  mcc={rep['mcc']:.4f}  "
            f"ece={rep['ece']:.4f}  auroc={rep['confidence_auroc']}"
        )
    print()

    # ---------------------------------------------------------------
    # Transfer check: leave-one-family-out.
    # The held-out mix is very different (hardship_request 3.3% -> 18.3%),
    # so a model that only works on the dev mix must be caught here.
    # ---------------------------------------------------------------
    print("E) leave-one-family-out transfer (train without the family, test on it)")
    fams = np.asarray([r["trajectory"]["case"]["family"] for r in dev])
    fn = models[best_name]
    rows = []
    for fam in sorted(set(fams)):
        test = fams == fam
        train = ~test
        if y[train].sum() in (0, train.sum()):
            continue
        model = fn()
        model.fit(X[train], y[train])
        prob = model.predict_proba(X[test])[:, 1]
        prob = np.where(gate[test], 0.02, prob)
        pred = (prob >= 0.5).astype(int)
        acc = float((pred == y[test]).mean())
        rows.append((fam, int(test.sum()), float(y[test].mean()), acc))
    for fam, n, gold, acc in sorted(rows, key=lambda r: r[3]):
        print(f"   {fam:<22} n={n:<3} gold_pass={gold:.3f}  accuracy={acc:.3f}")
    print(f"\n   mean LOFO accuracy = {np.mean([r[3] for r in rows]):.4f}")

    # ---------------------------------------------------------------
    # Where the errors live -- this decides what the LLM layer is for.
    # ---------------------------------------------------------------
    pred = (oof_gated >= 0.5).astype(int)
    wrong = pred != y
    print(f"\nF) {wrong.sum()} errors out of {len(y)}")
    per_fam = defaultdict(lambda: [0, 0])
    for i in range(len(y)):
        per_fam[fams[i]][1] += 1
        if wrong[i]:
            per_fam[fams[i]][0] += 1
    for fam, (bad, tot) in sorted(per_fam.items(), key=lambda kv: -kv[1][0] / kv[1][1]):
        print(f"   {fam:<22} {bad:>3}/{tot:<3} err={bad / tot:.3f}")
    fn_mask = (y == 1) & (pred == 0)
    fp_mask = (y == 0) & (pred == 1)
    print(f"\n   false 'fail' (gold pass, judged fail): {fn_mask.sum()}")
    print(f"   false 'pass' (gold fail, judged pass): {fp_mask.sum()}   <- the dangerous ones")

    # How much of the remaining error sits in a middling-probability band?
    # That band is what an LLM call should be spent on.
    for lo, hi in [(0.2, 0.8), (0.3, 0.7), (0.35, 0.65), (0.4, 0.6)]:
        band = (oof_gated > lo) & (oof_gated < hi)
        share_rows = band.mean()
        share_err = wrong[band].sum() / max(wrong.sum(), 1)
        print(
            f"   band {lo:.2f}-{hi:.2f}: {share_rows * 100:5.1f}% of rows carry "
            f"{share_err * 100:5.1f}% of errors"
        )

    np.save(os.path.join(ROOT, "experiments", "oof_structural.npy"), oof_gated)
    with open(os.path.join(ROOT, "experiments", "feature_names.json"), "w", encoding="utf-8") as h:
        json.dump(list(names), h, indent=1)
    print(f"\nbest structural model: {best_name}")


if __name__ == "__main__":
    main()
