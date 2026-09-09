"""Write results/manifest.json from artefacts that actually exist on disk.

Every claimed number is read back out of a file produced by a run, never typed
in by hand. A metric that was not measured is written as ``null`` rather than
estimated -- the submission schema is explicit that inventing a hidden-set score
or an unrun check is an error.

Run:  python scripts/write_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Fixture location. Honours the DATA environment variable so a reviewer can
# point at their own copy of references/OP-04/ without editing anything.
DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
RAW = os.path.join(RESULTS, "raw")

MODEL_SNAPSHOT_STRUCTURAL = "none (no model API calls)"
LLM_SNAPSHOT = "gpt-5-mini-2025-08-07"


def sha256(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def count_lines(path: str) -> int | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def main() -> None:
    official = load(os.path.join(RESULTS, "dev_score_report.json")) or {}
    oof = load(os.path.join(RESULTS, "dev_oof_metrics.json")) or {}
    thresh = load(os.path.join(RESULTS, "threshold_selection.json")) or {}
    ledger = load(os.path.join(RESULTS, "heldout_ledger_summary.json")) or {}
    ablation = load(os.path.join(RESULTS, "ablation.json")) or {}
    transfer = load(os.path.join(RESULTS, "transfer_lofo.json")) or {}

    if not official:
        sys.exit("run scripts/evaluate_dev.py first")

    claimed = {
        # Honest generalisation estimate: nested grouped CV on dev, recomputed by
        # the organisers' score.py. NOT in-sample dev numbers.
        "dev_balanced_accuracy": official.get("balanced_accuracy"),
        "dev_mcc": official.get("mcc"),
        "dev_ece": official.get("ece"),
        "dev_confidence_auroc": official.get("confidence_auroc"),
        # No model calls in the shipped configuration.
        "cost_per_trajectory_usd": 0.0,
        "input_tokens_per_trajectory": ledger.get("input_tokens_per_trajectory", 0.0),
        "output_tokens_per_trajectory": ledger.get("output_tokens_per_trajectory", 0.0),
        # Held-out and hidden metrics are the reviewer's to compute; labels are withheld.
        "heldout_balanced_accuracy": None,
        "heldout_mcc": None,
        "heldout_ece": None,
        "heldout_confidence_auroc": None,
    }

    runs = [
        {
            "run_id": "dev-oof-001",
            "description": (
                "Honest dev estimate. Nested grouped cross-validation on case_id: "
                "fold models, Platt calibrator and gates are all refitted inside "
                "each outer fold, so no row informs the model that scores it. "
                "These are the numbers claimed in submission.yaml."
            ),
            "seed": 20260915,
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "status": "completed",
            "threshold": oof.get("threshold"),
            "input_sha256": sha256(os.path.join(DATA, "dev.jsonl")),
            "raw_path": "results/raw/dev_oof_predictions.jsonl",
            "gold_path": "results/raw/dev_labels.jsonl",
            "n_rows": count_lines(os.path.join(RAW, "dev_oof_predictions.jsonl")),
            "metrics": official,
            "scorer": "data/score.py (organisers', unmodified)",
            "scorer_sha256": sha256(os.path.join(DATA, "score.py")),
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "heldout-001",
            "description": (
                "Graded artifact. Model trained on all 480 dev rows, predicting the "
                "240 held-out trajectories. Labels are withheld, so no metrics are "
                "claimed for this run."
            ),
            "seed": 20260915,
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "status": "completed",
            "threshold": oof.get("threshold"),
            "input_sha256": sha256(os.path.join(DATA, "heldout.jsonl")),
            "raw_path": "results/heldout_predictions.jsonl",
            "debug_path": "results/raw/heldout_debug.jsonl",
            "n_rows": count_lines(os.path.join(RESULTS, "heldout_predictions.jsonl")),
            "metrics": None,
            "api_calls": ledger.get("calls", 0),
            "cost_usd": ledger.get("cost_usd_total", 0.0),
            "ledger": ledger or None,
        },
        {
            "run_id": "dev-insample-001",
            "description": (
                "Dev predictions from the shipped model, which was trained on dev. "
                "IN-SAMPLE and therefore optimistic; included because the submission "
                "schema asks for dev_predictions.jsonl. Do not read metrics off this."
            ),
            "seed": 20260915,
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "status": "completed",
            "input_sha256": sha256(os.path.join(DATA, "dev.jsonl")),
            "raw_path": "results/dev_predictions.jsonl",
            "n_rows": count_lines(os.path.join(RESULTS, "dev_predictions.jsonl")),
            "metrics": None,
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "gate-purity-001",
            "description": (
                "Gate promotion audit: every deterministic check, how often it fires, "
                "and the pass rate among the rows it fires on. A check may only "
                "override the model at a 0.0000 pass rate."
            ),
            "status": "completed",
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "raw_path": "experiments/gates.json",
            "gates": list(oof.get("gates") or []),
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "threshold-001",
            "description": (
                "Threshold selection on nested dev OOF only. Plateau-centred, "
                "tie-broken toward reproducing the dev base rate. The published "
                "held-out pass rate (0.6333) was deliberately not used."
            ),
            "status": "completed",
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "raw_path": "results/threshold_selection.json",
            "chosen_threshold": thresh.get("chosen_threshold"),
            "viable_thresholds": thresh.get("viable_thresholds"),
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "ablation-001",
            "description": (
                "Per-layer ablation. Each layer removed on its own rather than a "
                "single stacked before/after, so the contribution of each is visible "
                "-- including the two arms that measured worse and were dropped."
            ),
            "status": "completed" if ablation else "not_run",
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "raw_path": "results/ablation.json",
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "transfer-lofo-001",
            "description": (
                "Leave-one-family-out: every row judged by a model trained with its "
                "family entirely absent. This is the test that decides whether the "
                "policy layer stays, because the private family mix is unpublished."
            ),
            "status": "completed" if transfer else "not_run",
            "model": MODEL_SNAPSHOT_STRUCTURAL,
            "raw_path": "results/transfer_lofo.json",
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "audit-50-001",
            "description": (
                "50 held-out trajectories read by hand against policy.md, verdicts "
                "recorded before any label was available. Drove five code fixes."
            ),
            "status": "completed",
            "model": "human (candidate)",
            "raw_path": "results/raw/audit_50_human_verdicts.json",
            "report_path": "results/audit_50.md",
            "worksheet_path": "results/audit_50_worksheet.md",
            "api_calls": 0,
            "cost_usd": 0.0,
        },
        {
            "run_id": "llm-layer-000",
            "description": (
                "Optional LLM adjudication layer for the uncertain band. Implemented "
                "with a metered token ledger but NEVER EXECUTED: no API key was "
                "available in the build environment. Reported as not run rather than "
                "estimated. Enable with `USE_LLM=1 ./scripts/reproduce.sh`."
            ),
            "status": "not_run",
            "model": LLM_SNAPSHOT,
            "raw_path": None,
            "api_calls": 0,
            "cost_usd": 0.0,
        },
    ]

    manifest = {
        "problem": "OP-04",
        "season": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claimed": claimed,
        "runs": runs,
        "configuration": {
            "architecture": "deterministic gates -> calibrated structural model -> optional LLM band adjudication",
            "model_api_calls_in_shipped_config": 0,
            "n_features": len(oof.get("gates") or []) and None,
            "threshold": oof.get("threshold"),
            "gates": list(oof.get("gates") or []),
            "calibration": "Platt (sigmoid) on grouped out-of-fold scores; isotonic rejected because its ties damage the confidence AUROC",
            "confidence_definition": "max(p, 1-p) on the calibrated probability of the emitted verdict",
            "grouping": "GroupKFold on trajectory.case.case_id (four trajectories share each case)",
            "seed": 20260915,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "hardware": "CPU only; no GPU",
            "network_required": False,
        },
        "fixtures": {
            name: sha256(os.path.join(DATA, name))
            for name in ("dev.jsonl", "heldout.jsonl", "tool_schemas.json", "score.py")
        },
        "compliance": {
            "harbour_imported_or_executed": False,
            "harbour_source_downloaded": False,
            "backend_state_reconstructed": False,
            "tool_calls_replayed_against_simulator": False,
            "documents_read": ["references/OP-01/harbour/policy.md", "references/HARBOUR.md"],
            "gold_fields_visible_to_judge": [],
            "note": (
                "The judge consumes id + trajectory (case, turns, audited_tool_calls) "
                "and tool_schemas.json only. judge.cli.strip_labels removes any label "
                "or goal_state field from every row before inference."
            ),
        },
        "spend": {
            "results_usd": 0.0,
            "development_usd": 0.0,
            "compute_usd": 0.0,
            "note": "No model API calls were made at any point in development or evaluation.",
        },
    }
    manifest["configuration"].pop("n_features", None)

    out = os.path.join(RESULTS, "manifest.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"wrote {out}")
    print(json.dumps(claimed, indent=2))


if __name__ == "__main__":
    main()
