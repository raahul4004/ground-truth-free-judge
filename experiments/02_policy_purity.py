"""Experiment 2 -- audit every deterministic check before trusting any of it.

For each structural and policy check, report how often it fires and the pass
rate among the rows it fires on. A check is only eligible to become a hard
gate if it fires on a useful number of rows AND has a 0.0000 pass rate --
i.e. it has never once condemned a run the database says succeeded.

Run:  python experiments/02_policy_purity.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from judge.featurize import extract_all  # noqa: E402
from judge.features import load_tool_schemas  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Fixture location. Honours the DATA environment variable so a reviewer can
# point at their own copy of references/OP-04/ without editing anything.
DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
MIN_GATE_SUPPORT = 5


def main() -> None:
    schemas = load_tool_schemas(os.path.join(DATA, "tool_schemas.json"))
    with open(os.path.join(DATA, "dev.jsonl"), encoding="utf-8") as handle:
        dev = [json.loads(line) for line in handle if line.strip()]

    feats = [extract_all(row, schemas) for row in dev]
    labels = [row["label"] for row in dev]
    base = sum(labels) / len(labels)
    print(f"dev n={len(dev)}  base pass rate={base:.4f}  features={len(feats[0])}\n")

    # Boolean-ish checks worth auditing: the structural gates and every policy
    # violation / missed-obligation flag.
    candidates = [
        name
        for name in feats[0]
        if name.startswith(("gate_", "pol_v_", "pol_m_"))
        or name in {"has_empty_assistant_turn", "has_schema_violation", "unverified_money_move",
                    "acted_on_closed_loan", "handoff_family_acted", "off_canonical_state_change",
                    "commit_claims_no_action", "escalate_and_state"}
    ]

    rows = []
    for name in candidates:
        fired = [labels[i] for i, f in enumerate(feats) if f.get(name, 0.0) > 0]
        if not fired:
            rows.append((name, 0, None, None))
            continue
        pass_rate = sum(fired) / len(fired)
        lift = pass_rate - base
        rows.append((name, len(fired), pass_rate, lift))

    print(f"{'check':<44} {'fires':>5} {'pass|fired':>11} {'lift':>8}  gate?")
    print("-" * 82)
    gates = []
    for name, n, pass_rate, lift in sorted(rows, key=lambda r: (r[2] is None, r[2], -r[1])):
        if n == 0:
            print(f"{name:<44} {n:>5} {'-':>11} {'-':>8}  never fires")
            continue
        eligible = pass_rate == 0.0 and n >= MIN_GATE_SUPPORT
        if eligible:
            gates.append(name)
        note = "GATE" if eligible else ("pure but thin" if pass_rate == 0.0 else "")
        print(f"{name:<44} {n:>5} {pass_rate:>11.4f} {lift:>+8.4f}  {note}")

    print(f"\ngate-eligible checks ({len(gates)}):")
    for g in gates:
        print(f"   {g}")

    # Union coverage of the eligible gates.
    hit = [
        i for i, f in enumerate(feats) if any(f.get(g, 0.0) > 0 for g in gates)
    ]
    if hit:
        covered = [labels[i] for i in hit]
        n_fail = len(labels) - sum(labels)
        print(
            f"\nunion fires on {len(hit)}/{len(dev)} rows, pass rate {sum(covered) / len(covered):.4f}"
            f"  -> catches {len(hit)}/{n_fail} = {len(hit) / n_fail:.3f} of all failures"
        )
        print("if every gate row is judged fail and everything else pass:")
        pred = [0 if i in set(hit) else 1 for i in range(len(dev))]
        tp = sum(1 for y, p in zip(labels, pred) if y == 1 and p == 1)
        tn = sum(1 for y, p in zip(labels, pred) if y == 0 and p == 0)
        pos, neg = sum(labels), len(labels) - sum(labels)
        print(f"   TPR={tp / pos:.4f}  TNR={tn / neg:.4f}  BA={(tp / pos + tn / neg) / 2:.4f}")

    with open(os.path.join(ROOT, "experiments", "gates.json"), "w", encoding="utf-8") as handle:
        json.dump(gates, handle, indent=1)


if __name__ == "__main__":
    main()
