"""Build the 50-row human audit worksheet from the held-out split.

Sampling is purposive, not uniform, and the design is stated so a reviewer can
judge it: the point of the audit is to find where the judge and a human reader
part company, and a uniform sample of a set the judge gets ~85% right spends
most of its rows confirming easy agreements.

    20 rows  lowest confidence            -- where the judge itself is unsure
    10 rows  gate-decided                 -- verify the "certain failure" claim
    10 rows  confident FAIL verdicts      -- the expensive direction to be wrong in
    10 rows  seeded-random across families -- an unbiased control on the rest

Held-out labels are not available to the candidate, so the worksheet has no
gold column. The reviewer supplies the database label as the third opinion;
that is the three-way comparison the bar asks for.

Run:  python scripts/make_audit_sample.py
"""

from __future__ import annotations

import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from judge.features import ParsedTrajectory  # noqa: E402
from judge.llm import render_steps  # noqa: E402

# Fixture location. Honours the DATA environment variable so a reviewer can
# point at their own copy of references/OP-04/ without editing anything.
DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
SEED = 20260915
N_LOW_CONF, N_GATE, N_CONF_FAIL, N_RANDOM = 20, 10, 10, 10


def main() -> None:
    with open(os.path.join(DATA, "heldout.jsonl"), encoding="utf-8") as h:
        rows = {json.loads(line)["id"]: json.loads(line) for line in h if line.strip()}
    with open(os.path.join(RESULTS, "raw", "heldout_debug.jsonl"), encoding="utf-8") as h:
        preds = {json.loads(line)["id"]: json.loads(line) for line in h if line.strip()}

    ids = [i for i in preds if i in rows]
    rng = random.Random(SEED)

    chosen: list[tuple[str, str]] = []
    taken: set[str] = set()
    recorded: dict | None = None

    # If the audit has already been carried out, the worksheet must show exactly
    # the rows that were read -- otherwise the rendered evidence drifts away from
    # the recorded verdicts every time the model is retrained.
    recorded_path = os.path.join(RESULTS, "raw", "audit_50_human_verdicts.json")
    if os.path.exists(recorded_path):
        with open(recorded_path, encoding="utf-8") as fh:
            recorded = json.load(fh)["verdicts"]
        for row_id in recorded:
            if row_id in preds and row_id in rows:
                chosen.append((row_id, "audited"))
                taken.add(row_id)
        print(f"pinned to {len(chosen)} previously audited rows")

    budget = max(sum((N_LOW_CONF, N_GATE, N_CONF_FAIL, N_RANDOM)), len(taken))

    def take(pool: list[str], n: int, reason: str) -> None:
        if len(taken) >= budget:
            return
        for row_id in pool:
            if n <= 0 or len(taken) >= budget:
                return
            if row_id in taken:
                continue
            chosen.append((row_id, reason))
            taken.add(row_id)
            n -= 1

    by_conf = sorted(ids, key=lambda i: preds[i]["confidence"])
    take(by_conf, N_LOW_CONF, "lowest confidence")

    gate_rows = [i for i in ids if preds[i]["gate_fired"]]
    rng.shuffle(gate_rows)
    take(gate_rows, N_GATE, "gate-decided")

    conf_fail = sorted(
        (i for i in ids if preds[i]["verdict"] == 0 and not preds[i]["gate_fired"]),
        key=lambda i: -preds[i]["confidence"],
    )
    take(conf_fail, N_CONF_FAIL, "confident FAIL")

    rest = [i for i in ids if i not in taken]
    rng.shuffle(rest)
    take(rest, N_RANDOM, "seeded random")

    out_path = os.path.join(RESULTS, "audit_50_worksheet.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("# Audit worksheet -- 50 held-out decisions\n\n")
        fh.write(
            f"Design: {N_LOW_CONF} lowest-confidence, {N_GATE} gate-decided, "
            f"{N_CONF_FAIL} confident-FAIL, {N_RANDOM} seeded-random (seed {SEED}). "
            "Rows marked `audited` are pinned to the set actually read, so this "
            "worksheet keeps matching the recorded verdicts across retrains.\n"
            "Held-out labels are withheld from the candidate; the reviewer supplies "
            "the database column.\n\n"
        )
        for n, (row_id, reason) in enumerate(chosen, 1):
            row, pred = rows[row_id], preds[row_id]
            parsed = ParsedTrajectory(row)
            fh.write(f"---\n\n## {n}. `{row_id}`\n\n")
            fh.write(
                f"- sampled because: **{reason}**\n"
                f"- family / difficulty: `{parsed.family}` / `{parsed.difficulty}`\n"
                f"- judge verdict: **{pred['verdict']}** "
                f"(p={pred['probability']:.3f}, confidence={pred['confidence']:.3f})\n"
                f"- judge category: `{pred['category']}`\n"
                f"- all categories: {', '.join(f'`{c}`' for c in pred['all_categories'])}\n"
                f"- gate fired: {pred['gate_fired']}\n\n"
            )
            fh.write(f"**Customer message**\n\n> {parsed.message}\n\n")
            fh.write("**Run**\n\n```\n" + render_steps(parsed, result_limit=260) + "\n```\n\n")
            fh.write(f"**Audit log**: `{', '.join(parsed.audited) or '(none)'}`\n\n")
            mine = (recorded or {}).get(row_id) if reason == "audited" else None
            if mine:
                agree = "yes" if mine["v"] == pred["verdict"] else "**NO**"
                fh.write(
                    f"**My verdict**: **{mine['v']}** · **Agree with judge?** {agree} · "
                    f"**Why**: {mine['reason']}\n\n"
                )
            else:
                fh.write("**My verdict**: _TBD_ · **Agree with judge?** _TBD_ · **Note**: _TBD_\n\n")

    with open(os.path.join(RESULTS, "raw", "audit_50_sample.json"), "w", encoding="utf-8") as fh:
        json.dump(
            [{"id": i, "reason": r, "judge": preds[i]} for i, r in chosen], fh, indent=1
        )

    print(f"wrote {out_path} ({len(chosen)} rows)")
    from collections import Counter

    print("  by reason:", dict(Counter(r for _i, r in chosen)))
    print("  by family:", dict(Counter(rows[i]["trajectory"]["case"]["family"] for i, _r in chosen)))
    print("  judge verdicts:", dict(Counter(preds[i]["verdict"] for i, _r in chosen)))


if __name__ == "__main__":
    main()
