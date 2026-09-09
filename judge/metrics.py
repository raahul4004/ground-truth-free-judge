"""Metrics mirroring the published ``score.py``.

These exist so experiments can be run offline without shelling out. Every
number that ends up in a report or the manifest is recomputed with the
organisers' own ``score.py`` -- this module is for the inner loop only, and
``scripts/check_metrics_parity.py`` asserts the two agree.
"""

from __future__ import annotations

from collections import Counter


def balanced_accuracy(y: list[int], p: list[int]) -> float:
    tp = sum(1 for a, b in zip(y, p) if a == 1 and b == 1)
    tn = sum(1 for a, b in zip(y, p) if a == 0 and b == 0)
    pos = sum(y)
    neg = len(y) - pos
    tpr = tp / pos if pos else 0.0
    tnr = tn / neg if neg else 0.0
    return (tpr + tnr) / 2


def mcc(y: list[int], p: list[int]) -> float:
    tp = sum(1 for a, b in zip(y, p) if a == 1 and b == 1)
    tn = sum(1 for a, b in zip(y, p) if a == 0 and b == 0)
    fp = sum(1 for a, b in zip(y, p) if a == 0 and b == 1)
    fn = sum(1 for a, b in zip(y, p) if a == 1 and b == 0)
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return (tp * tn - fp * fn) / denom if denom else 0.0


def auroc(correct: list[int], conf: list[float]) -> float | None:
    """P(confidence of a correct verdict > confidence of an incorrect one).

    Ties count half, so constant confidence scores exactly 0.5. This is the
    bar the reference judge misses (0.5563), and the one this design targets.
    """
    pos = [c for c, k in zip(conf, correct) if k == 1]
    neg = [c for c, k in zip(conf, correct) if k == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else 0.5 if a == b else 0.0
    return wins / (len(pos) * len(neg))


def ece(y: list[int], p: list[int], conf: list[float], bins: int = 10) -> float:
    total = 0.0
    n = len(y)
    if n == 0:
        return 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(conf) if (lo <= c < hi) or (b == bins - 1 and c == 1.0)]
        if not idx:
            continue
        acc = sum(1 for i in idx if y[i] == p[i]) / len(idx)
        avg_conf = sum(conf[i] for i in idx) / len(idx)
        total += (len(idx) / n) * abs(acc - avg_conf)
    return total


def cohens_kappa(y: list[int], p: list[int]) -> float:
    n = len(y)
    if n == 0:
        return 0.0
    po = sum(1 for a, b in zip(y, p) if a == b) / n
    cy, cp = Counter(y), Counter(p)
    pe = sum((cy[k] / n) * (cp[k] / n) for k in (0, 1))
    return (po - pe) / (1 - pe) if pe != 1 else 1.0


def report(y: list[int], p: list[int], conf: list[float], bins: int = 10) -> dict:
    correct = [1 if a == b else 0 for a, b in zip(y, p)]
    auc = auroc(correct, conf)
    return {
        "n": len(y),
        "balanced_accuracy": round(balanced_accuracy(y, p), 4),
        "mcc": round(mcc(y, p), 4),
        "confidence_auroc": round(auc, 4) if auc is not None else None,
        "ece": round(ece(y, p, conf, bins), 4),
        "cohens_kappa": round(cohens_kappa(y, p), 4),
        "accuracy": round(sum(correct) / len(y), 4) if y else None,
        "predicted_pass_rate": round(sum(p) / len(p), 4) if p else None,
        "gold_pass_rate": round(sum(y) / len(y), 4) if y else None,
    }


BARS = {
    "balanced_accuracy": ("min", 0.79),
    "mcc": ("min", 0.65),
    "ece": ("max", 0.10),
    "confidence_auroc": ("min", 0.65),
}


def bar_status(rep: dict) -> dict[str, bool | None]:
    out: dict[str, bool | None] = {}
    for key, (direction, threshold) in BARS.items():
        value = rep.get(key)
        if value is None:
            out[key] = None
        elif direction == "min":
            out[key] = value >= threshold
        else:
            out[key] = value <= threshold
    return out


def format_report(rep: dict, label: str = "") -> str:
    status = bar_status(rep)
    mark = {True: "PASS", False: "FAIL", None: "n/a "}
    lines = [f"{label}  n={rep['n']}"]
    for key, (_direction, threshold) in BARS.items():
        value = rep.get(key)
        shown = "None" if value is None else f"{value:.4f}"
        lines.append(f"   {mark[status[key]]}  {key:<20} {shown:>8}   bar {threshold}")
    lines.append(
        f"        accuracy={rep['accuracy']} kappa={rep['cohens_kappa']} "
        f"pred_pass={rep['predicted_pass_rate']} gold_pass={rep['gold_pass_rate']}"
    )
    return "\n".join(lines)
