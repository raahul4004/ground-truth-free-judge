"""Pre-submission preflight: check this package against SUBMISSION_SCHEMA.md.

Fails loudly rather than warning quietly. Every check corresponds to something
the schema or the problem brief requires, or to something a reviewer's automated
intake would reject.

Run:  python scripts/preflight.py
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULTS = os.path.join(ROOT, "results")
RAW = os.path.join(RESULTS, "raw")

errors: list[str] = []
warnings: list[str] = []
oks: list[str] = []


def check(cond: bool, ok_msg: str, err_msg: str, fatal: bool = True) -> bool:
    if cond:
        oks.append(ok_msg)
    else:
        (errors if fatal else warnings).append(err_msg)
    return cond


def main() -> None:
    # --- 1. required common files -------------------------------------
    required = [
        "README.md", "EXPERIMENT_LOG.md", "DECISIONS.md", "LANDSCAPE.md",
        "MEMO.md", "LICENSE", "scripts/reproduce.sh", "results/manifest.json",
    ]
    for rel in required:
        p = os.path.join(ROOT, rel)
        check(
            os.path.exists(p) and os.path.getsize(p) > 0,
            f"present and non-empty: {rel}",
            f"MISSING or empty required file: {rel}",
        )
    check(
        os.path.isdir(RAW) and any(os.scandir(RAW)),
        "results/raw/ is non-empty",
        "results/raw/ must be non-empty",
    )
    check(
        os.access(os.path.join(ROOT, "scripts/reproduce.sh"), os.X_OK),
        "scripts/reproduce.sh is executable",
        "scripts/reproduce.sh is not executable (chmod +x)",
    )

    # --- 2. OP-04 required artifacts ----------------------------------
    for rel in ("results/dev_predictions.jsonl", "results/heldout_predictions.jsonl",
                "results/audit_50.md"):
        check(os.path.exists(os.path.join(ROOT, rel)) and os.path.getsize(os.path.join(ROOT, rel)) > 0,
              f"present: {rel}", f"MISSING OP-04 artifact: {rel}")

    # --- 3. prediction schema -----------------------------------------
    for rel in ("results/dev_predictions.jsonl", "results/heldout_predictions.jsonl",
                "results/raw/dev_oof_predictions.jsonl"):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        seen: set[str] = set()
        bad = 0
        n = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if set(o) != {"id", "verdict", "confidence", "category"}:
                    bad += 1
                elif o["verdict"] not in (0, 1) or not isinstance(o["verdict"], int):
                    bad += 1
                elif not isinstance(o["confidence"], (int, float)) or not 0 <= o["confidence"] <= 1:
                    bad += 1
                elif not isinstance(o["category"], str) or not o["category"]:
                    bad += 1
                if o.get("id") in seen:
                    bad += 1
                seen.add(o.get("id"))
        check(bad == 0, f"{rel}: {n} rows, schema clean, no duplicate ids",
              f"{rel}: {bad} malformed rows")

    # --- 4. claimed numbers must match the manifest AND the scorer -----
    manifest = json.load(open(os.path.join(RESULTS, "manifest.json"), encoding="utf-8"))
    official = json.load(open(os.path.join(RESULTS, "dev_score_report.json"), encoding="utf-8"))
    claimed = manifest.get("claimed", {})

    for key, scorer_key in (
        ("dev_balanced_accuracy", "balanced_accuracy"),
        ("dev_mcc", "mcc"),
        ("dev_ece", "ece"),
        ("dev_confidence_auroc", "confidence_auroc"),
    ):
        check(
            claimed.get(key) == official.get(scorer_key),
            f"claimed {key} == score.py {scorer_key} ({claimed.get(key)})",
            f"claimed {key}={claimed.get(key)} disagrees with score.py "
            f"{scorer_key}={official.get(scorer_key)}",
        )

    # submission.yaml is uploaded through the private form and must NOT be part
    # of the evaluated commit (it would reference its own SHA). Look for it in
    # the repo root and alongside it, and require that it is gitignored if it is
    # sitting in the tree.
    sub_path = os.path.join(ROOT, "submission.yaml")
    if not os.path.exists(sub_path):
        sub_path = os.path.join(os.path.dirname(ROOT), "submission.yaml")
    if os.path.exists(sub_path):
        if os.path.dirname(os.path.abspath(sub_path)) == os.path.abspath(ROOT):
            ignored = "submission.yaml" in open(
                os.path.join(ROOT, ".gitignore"), encoding="utf-8"
            ).read().split()
            check(
                ignored,
                "submission.yaml is in the tree but gitignored (not committed)",
                "submission.yaml sits in the repo and is NOT gitignored -- it must not "
                "be part of the evaluated commit (self-referencing SHA)",
            )
        text = open(sub_path, encoding="utf-8").read()
        check(len(text.encode()) <= 64 * 1024, "submission.yaml <= 64 KB",
              "submission.yaml exceeds 64 KB")
        for key in ("dev_balanced_accuracy", "dev_mcc", "dev_ece", "dev_confidence_auroc"):
            m = re.search(rf"^\s*{key}:\s*([0-9.]+)", text, re.M)
            if not m:
                errors.append(f"submission.yaml missing {key}")
                continue
            check(
                abs(float(m.group(1)) - float(claimed[key])) < 1e-9,
                f"submission.yaml {key} matches manifest",
                f"submission.yaml {key}={m.group(1)} != manifest {claimed[key]}",
            )
        for field in ("repo_visibility: private", "leaderboard: false",
                      "problem: OP-04", "season: 1"):
            check(field in text, f"submission.yaml has `{field}`",
                  f"submission.yaml must contain `{field}`")
        placeholders = re.findall(r"REPLACE_WITH_[A-Z_0-9]+", text)
        check(not placeholders, "submission.yaml has no placeholders",
              f"submission.yaml still has placeholders: {sorted(set(placeholders))} "
              "(handle/repo/commit must be filled in before upload)", fatal=False)

    # --- 5. manifest structure ----------------------------------------
    runs = manifest.get("runs") or []
    check(bool(runs), f"manifest has {len(runs)} runs", "manifest.runs must be non-empty")
    ids = [r.get("run_id") for r in runs]
    check(len(ids) == len(set(ids)), "manifest run_ids are unique",
          f"duplicate run_ids: {[i for i in ids if ids.count(i) > 1]}")
    for r in runs:
        rp = r.get("raw_path")
        if rp:
            check(os.path.exists(os.path.join(ROOT, rp)),
                  f"run {r['run_id']}: raw_path exists", f"run {r['run_id']}: missing {rp}")

    def finite(o) -> bool:
        if isinstance(o, float):
            return o == o and o not in (float("inf"), float("-inf"))
        if isinstance(o, dict):
            return all(finite(v) for v in o.values())
        if isinstance(o, list):
            return all(finite(v) for v in o)
        return True

    check(finite(manifest), "manifest contains no NaN/Infinity",
          "manifest contains NaN or Infinity")

    # --- 6. no unsafe paths, no symlinks ------------------------------
    bad_paths = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "data", "brief"}]
        for name in filenames + dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                bad_paths.append(os.path.relpath(full, ROOT))
    check(not bad_paths, "no symlinks anywhere in the package",
          f"symlinks present (schema forbids): {bad_paths}")

    # --- 7. every module imports and compiles -------------------------
    for pkg_dir in ("judge", "scripts", "experiments"):
        for name in sorted(os.listdir(os.path.join(ROOT, pkg_dir))):
            if not name.endswith(".py"):
                continue
            path = os.path.join(ROOT, pkg_dir, name)
            try:
                ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError as exc:
                errors.append(f"syntax error in {pkg_dir}/{name}: {exc}")
    for mod in ("judge.features", "judge.policy", "judge.intent", "judge.featurize",
                "judge.model", "judge.taxonomy", "judge.metrics", "judge.llm",
                "judge.api", "judge.cli"):
        try:
            importlib.import_module(mod)
            oks.append(f"imports cleanly: {mod}")
        except Exception as exc:
            errors.append(f"import failed: {mod}: {type(exc).__name__}: {exc}")

    # --- 8. compliance: nothing touches Harbour ------------------------
    forbidden = re.compile(r"\b(import\s+harbour|from\s+harbour|harbour\.(?:app|backend|tools|policy))",
                           re.I)
    hits = []
    for pkg_dir in ("judge", "scripts", "experiments"):
        for name in os.listdir(os.path.join(ROOT, pkg_dir)):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(ROOT, pkg_dir, name), encoding="utf-8").read()
            if forbidden.search(src):
                hits.append(f"{pkg_dir}/{name}")
    check(not hits, "no code imports or references Harbour modules",
          f"Harbour reference found in: {hits}")

    harbour_src = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for name in filenames:
            if name in {"backend.py", "app.py", "tools.py", "seed_data.py", "seed.json"}:
                harbour_src.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    check(not harbour_src, "no Harbour source files on disk",
          f"Harbour source present: {harbour_src}")

    # --- 9. the judge must never receive a gold field ------------------
    from judge.cli import strip_labels
    probe = {"id": "x", "label": 1, "goal_state": {"a": 1}, "trajectory": {}}
    stripped = strip_labels([probe])[0]
    check(
        "label" not in stripped and "goal_state" not in stripped,
        "strip_labels removes label and goal_state before inference",
        "strip_labels leaked a gold field",
    )

    # --- 10. gate purity must still hold ------------------------------
    gates_path = os.path.join(ROOT, "experiments", "gates.json")
    if os.path.exists(gates_path):
        gates = json.load(open(gates_path, encoding="utf-8"))
        model_gates = list(manifest.get("configuration", {}).get("gates") or [])
        check(
            sorted(gates) == sorted(model_gates),
            f"gates.json ({len(gates)}) matches the manifest's gate list",
            f"gate list drift: gates.json={sorted(gates)} manifest={sorted(model_gates)}",
        )

    # --- report -------------------------------------------------------
    print(f"preflight: {len(oks)} passed, {len(warnings)} warnings, {len(errors)} errors\n")
    if warnings:
        print("WARNINGS (must be resolved before upload, not blocking locally):")
        for w in warnings:
            print(f"  ! {w}")
        print()
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  x {e}")
        print()
        sys.exit(1)
    print("No blocking errors.")


if __name__ == "__main__":
    main()
