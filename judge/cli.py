"""Unattended CLI for the OP-04 judge.

    python -m judge.cli train   --dev data/dev.jsonl --out artifacts/structural.pkl
    python -m judge.cli predict --input data/heldout.jsonl --out results/heldout_predictions.jsonl
    python -m judge.cli predict --input data/heldout.jsonl --out preds.jsonl --use-llm

``predict`` is the graded path: it reads a JSONL of trajectories, writes one
prediction object per line, and needs no service, no GPU and no network unless
``--use-llm`` is passed. Any ``label`` field present in the input is dropped
before the judge sees the row, so a labelled file cannot leak an answer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from judge.api import DEFAULT_BAND, Judge  # noqa: E402
from judge.features import load_tool_schemas  # noqa: E402
from judge.llm import LLMAdjudicator, Ledger  # noqa: E402
from judge.model import StructuralJudge, load_gates  # noqa: E402

PRED_FIELDS = ("id", "verdict", "confidence", "category")


def read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for n, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                sys.exit(f"{path}:{n}: invalid JSON ({exc})")
    return rows


def strip_labels(rows: list[dict]) -> list[dict]:
    """Remove any gold field before inference. Defensive, not cosmetic."""
    out = []
    for row in rows:
        clean = {k: v for k, v in row.items() if k not in ("label", "goal_state")}
        out.append(clean)
    return out


def cmd_train(args: argparse.Namespace) -> None:
    schemas = load_tool_schemas(args.schemas)
    rows = read_jsonl(args.dev)
    if any("label" not in r for r in rows):
        sys.exit("training requires a `label` on every row")
    gates = load_gates(args.gates) if args.gates else None
    judge = StructuralJudge(gates=gates or StructuralJudge().gates, threshold=args.threshold)
    judge.fit(rows, schemas)
    judge.save(args.out)
    print(
        f"trained on {len(rows)} rows, {len(judge.names)} features, "
        f"{len(judge.models)} folds -> {args.out}"
    )
    print(f"gates ({len(judge.gates)}): {', '.join(judge.gates)}")


def cmd_predict(args: argparse.Namespace) -> None:
    schemas = load_tool_schemas(args.schemas)
    raw = read_jsonl(args.input)
    rows = strip_labels(raw)

    ledger = Ledger(path=args.ledger)
    llm = None
    if args.use_llm:
        llm = LLMAdjudicator(model=args.llm_model, ledger=ledger)
        if not llm.available():
            sys.exit(
                "--use-llm requested but no OPENAI_API_KEY / LLM_API_KEY is set. "
                "Run without --use-llm for the zero-token structural judge."
            )

    judge = Judge(
        model_path=args.model,
        schemas=schemas,
        use_llm=args.use_llm,
        band=(args.band_low, args.band_high),
        llm=llm,
    )

    started = time.time()
    results = judge.judge_rows(rows)
    elapsed = time.time() - started

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        for r in results:
            handle.write(json.dumps({k: r[k] for k in PRED_FIELDS}) + "\n")

    if args.debug_out:
        os.makedirs(os.path.dirname(args.debug_out) or ".", exist_ok=True)
        with open(args.debug_out, "w", encoding="utf-8") as handle:
            for r in results:
                handle.write(json.dumps(r) + "\n")

    n = len(results)
    gated = sum(1 for r in results if r["gate_fired"])
    consulted = sum(1 for r in results if r["llm_consulted"])
    print(f"wrote {n} predictions -> {args.out}")
    print(f"  predicted pass rate : {sum(r['verdict'] for r in results) / max(n, 1):.4f}")
    print(f"  gate-decided rows   : {gated} ({gated / max(n, 1):.1%})")
    print(f"  llm-consulted rows  : {consulted} ({consulted / max(n, 1):.1%})")
    print(f"  wall time           : {elapsed:.1f}s ({elapsed / max(n, 1):.3f}s per trajectory)")
    totals = ledger.totals(n)
    print(f"  ledger              : {json.dumps(totals)}")
    if args.ledger_summary:
        with open(args.ledger_summary, "w", encoding="utf-8") as handle:
            json.dump(totals, handle, indent=2)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="judge", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    schemas_default = os.environ.get("SCHEMAS") or os.path.join(
        os.environ.get("DATA") or os.path.join(ROOT, "data"), "tool_schemas.json"
    )

    t = sub.add_parser("train", help="fit the structural judge on a labelled split")
    t.add_argument("--schemas", default=schemas_default)
    t.add_argument("--dev", default=os.path.join(ROOT, "data", "dev.jsonl"))
    t.add_argument("--out", default=os.path.join(ROOT, "artifacts", "structural.pkl"))
    t.add_argument("--gates", default=os.path.join(ROOT, "experiments", "gates.json"))
    t.add_argument("--threshold", type=float, default=0.525,
                   help="selected on dev by experiments/03_threshold.py")
    t.set_defaults(func=cmd_train)

    p = sub.add_parser("predict", help="judge a JSONL of trajectories")
    p.add_argument("--schemas", default=schemas_default)
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--model", default=os.path.join(ROOT, "artifacts", "structural.pkl"))
    p.add_argument("--debug-out", default=None, help="richer per-row record for analysis")
    p.add_argument("--use-llm", action="store_true", help="consult a budget-class model on band rows")
    p.add_argument("--llm-model", default="gpt-5-mini-2025-08-07")
    p.add_argument("--band-low", type=float, default=DEFAULT_BAND[0])
    p.add_argument("--band-high", type=float, default=DEFAULT_BAND[1])
    p.add_argument("--ledger", default=None, help="append per-call token/cost rows here")
    p.add_argument("--ledger-summary", default=None)
    p.set_defaults(func=cmd_predict)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
