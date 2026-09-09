#!/usr/bin/env bash
# Reproduce every number in README.md, submission.yaml and results/manifest.json.
#
# CPU only, no GPU, no service to stand up, and no network unless USE_LLM=1.
# Nothing here imports, executes or vendors Harbour.
#
#   ./scripts/reproduce.sh              # full pipeline (~4 minutes)
#   DATA=/path/to/references/OP-04 ./scripts/reproduce.sh
#   USE_LLM=1 OPENAI_API_KEY=sk-... ./scripts/reproduce.sh
#
# Subcommands, if you want the steps separately rather than a server that
# blocks the evaluation:
#   python -m judge.cli train   --dev <dev.jsonl>  --out artifacts/structural.pkl
#   python -m judge.cli predict --input <in.jsonl> --out <preds.jsonl>

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DATA="${DATA:-$ROOT/data}"
PY="${PY:-python3}"
USE_LLM="${USE_LLM:-0}"

DEV="$DATA/dev.jsonl"
HELDOUT="$DATA/heldout.jsonl"
SCHEMAS="$DATA/tool_schemas.json"
SCORER="$DATA/score.py"

echo "=== 0. environment ==="
$PY --version
for f in "$DEV" "$HELDOUT" "$SCHEMAS" "$SCORER"; do
  [ -f "$f" ] || { echo "missing fixture: $f (set DATA=/path/to/references/OP-04)"; exit 1; }
done
$PY - <<'EOF'
import hashlib, os, json
data = os.environ.get("DATA") or "data"
out = {}
for name in ("dev.jsonl", "heldout.jsonl", "tool_schemas.json", "score.py"):
    p = os.path.join(data, name)
    with open(p, "rb") as fh:
        out[name] = hashlib.sha256(fh.read()).hexdigest()[:16]
print("fixture sha256 (first 16): " + json.dumps(out, indent=1))
EOF
mkdir -p artifacts results/raw
cp -f "$SCHEMAS" artifacts/tool_schemas.json

echo
echo "=== 1. gate purity audit (a gate must never fire on a passing run) ==="
$PY experiments/02_policy_purity.py | tail -20

echo
echo "=== 2. honest dev evaluation: nested grouped CV + the official score.py ==="
$PY scripts/evaluate_dev.py

echo
echo "=== 3. threshold selection (dev only) ==="
$PY experiments/03_threshold.py | tail -12

echo
echo "=== 4. train the shipped model on all of dev ==="
$PY -m judge.cli train --schemas "$SCHEMAS" --dev "$DEV" --out artifacts/structural.pkl

echo
echo "=== 5. predict held-out (the graded artifact) ==="
LLM_FLAGS=()
if [ "$USE_LLM" = "1" ]; then
  echo "    layer 3 enabled: consulting a budget-class model on band rows"
  LLM_FLAGS=(--use-llm --ledger results/raw/ledger.jsonl)
fi
$PY -m judge.cli predict --schemas "$SCHEMAS" --input "$HELDOUT" \
  --out results/heldout_predictions.jsonl \
  --debug-out results/raw/heldout_debug.jsonl \
  --ledger-summary results/heldout_ledger_summary.json "${LLM_FLAGS[@]+"${LLM_FLAGS[@]}"}"

echo
echo "=== 6. predict dev (artifact; in-sample, NOT the reported dev metrics) ==="
$PY -m judge.cli predict --schemas "$SCHEMAS" --input "$DEV" \
  --out results/dev_predictions.jsonl --debug-out results/raw/dev_debug.jsonl

echo
echo "=== 7. score the honest dev predictions with the organisers' scorer ==="
$PY "$SCORER" --pred results/raw/dev_oof_predictions.jsonl \
              --gold results/raw/dev_labels.jsonl \
              --report results/dev_score_report.json

echo
echo "=== 8. per-layer ablation (slow, ~2 min; set SKIP_ABLATION=1 to skip) ==="
if [ "${SKIP_ABLATION:-0}" = "1" ]; then
  echo "    skipped"
else
  $PY experiments/04_ablation.py | tail -14
fi

echo
echo "=== 9. rebuild the 50-decision audit and the run manifest ==="
$PY scripts/make_audit_sample.py
$PY scripts/write_audit_50.py
$PY scripts/write_manifest.py

echo
echo "=== 10. preflight: check the package against SUBMISSION_SCHEMA.md ==="
$PY scripts/preflight.py

echo
echo "=== done ==="
echo "  results/heldout_predictions.jsonl  <- the graded predictions"
echo "  results/dev_score_report.json      <- official scorer output on dev"
echo "  results/audit_50.md                <- 50 hand-read decisions"
echo "  results/manifest.json              <- run records and claimed metrics"
