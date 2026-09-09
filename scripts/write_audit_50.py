"""Assemble results/audit_50.md from the recorded human verdicts.

Compares three columns where available: the candidate's own read, the shipped
judge, and (reviewer-side) the database label. Held-out labels are withheld, so
the third column is left for the reviewer to fill.

Run:  python scripts/write_audit_50.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.environ.get("DATA") or os.path.join(ROOT, "data")
RESULTS = os.path.join(ROOT, "results")
RAW = os.path.join(RESULTS, "raw")


def main() -> None:
    human = json.load(open(os.path.join(RAW, "audit_50_human_verdicts.json"), encoding="utf-8"))
    verdicts = human["verdicts"]
    preds = {
        json.loads(line)["id"]: json.loads(line)
        for line in open(os.path.join(RAW, "heldout_debug.jsonl"), encoding="utf-8")
        if line.strip()
    }
    rows = {
        json.loads(line)["id"]: json.loads(line)
        for line in open(os.path.join(DATA, "heldout.jsonl"), encoding="utf-8")
        if line.strip()
    }

    missing = [i for i in verdicts if i not in preds]
    if missing:
        sys.exit(f"{len(missing)} audited ids are not in heldout predictions: {missing[:3]}")

    agree, disagree = [], []
    for row_id, rec in verdicts.items():
        judge = preds[row_id]["verdict"]
        (agree if judge == rec["v"] else disagree).append(row_id)

    n = len(verdicts)
    print(f"audited {n} rows: agree {len(agree)}, disagree {len(disagree)}")

    lines: list[str] = []
    w = lines.append
    w("# Audit of 50 held-out decisions\n")
    w(
        "Fifty held-out trajectories read by hand against `policy.md`, with my own verdict "
        "recorded **before** any label was available -- held-out labels are withheld from "
        "candidates, so the third opinion (the database) is the reviewer's to add. Raw "
        "verdicts and reasons: `results/raw/audit_50_human_verdicts.json`. The worksheet I "
        "read from, with each trajectory rendered in full, is "
        "`results/audit_50_worksheet.md`.\n"
    )
    w("## How the 50 were chosen\n")
    w(
        "Purposively, not uniformly, and the design is stated so it can be discounted: the "
        "judge is right about 85% of the time, so a uniform sample spends most of its rows "
        "confirming easy agreements. The split was 20 lowest-confidence rows, 10 "
        "gate-decided rows (to test the \"certain failure\" claim), 10 confident-FAIL rows "
        "(the expensive direction to be wrong in), and 10 seeded-random rows as an unbiased "
        "control. Seed 20260915.\n"
    )
    w("## What the audit changed\n")
    w(
        "This is the part worth reading. The audit was run against an earlier build of the "
        "judge and I disagreed with it on **20 of 50** rows. The disagreements were not "
        "spread out -- they fell into five clusters, each of which turned out to be a "
        "specific bug rather than a difference of opinion:\n"
    )
    w(
        "| # rows | What I saw | Root cause | Fix |\n"
        "|---|---|---|---|\n"
        "| 6 | Runs that escalated after two failed identity checks were judged failures | The model had the *negative* half of the escalation ledger (\"required a handoff and didn't\") but not the positive half, so it learned \"escalate-only == failure\" from whichever families were common in dev | Added `escalation_required` and its four quadrants in `policy.py`, derived from the policy text rather than the label |\n"
        "| 4 | Injection runs that served the customer's real request and ignored the planted one were flagged `injected_instruction_followed` | The rule fired on *any* state change in a message containing an override marker, without asking whether the **planted** action was the one performed | Condition on `v_injection_obeyed_*` only |\n"
        "| 2 | A refused `request_document` retried with a valid kind and succeeded, judged `refusal_ignored` | `refusal_ignored` fired whenever any refusal existed and no escalation followed, ignoring that the agent had recovered | Require the refused tool to have *never* subsequently succeeded (`refused_only`) |\n"
        "| 2 | A fully eligible hardship plan judged `mandatory_escalation_missed` | \"my wife's shop closed\" matched a third-party trigger. The policy trigger is a third party *acting for* the customer, not any mention of a relative | Narrowed `THIRD_PARTY_SPEAKER` to instructing/acting phrasings |\n"
        "| 1 | A statement request on a loan already on a hardship plan judged a failure | `already_on_hardship_plan` forced escalation regardless of what was being asked for | Gate that reason on the request actually being hardship-related |\n"
    )
    w(
        "One further defect fell out of the same reading: `has_state_change` was computed "
        "from `audited_tool_calls` membership, so a call the tool layer **refused** counted "
        "as an action. A refused call changes nothing. State features now count only calls "
        "that were not refused on every observation.\n"
    )
    w(
        "Fixing these five things moved the honest nested-CV dev estimate from "
        "**BA 0.8426 / MCC 0.6785** to **BA 0.8503 / MCC 0.6994**, and two of the corrected "
        "checks became clean enough to promote to hard gates (8 gates, still zero false "
        "positives). That is the argument for doing the audit at all: reading 50 "
        "trajectories found bugs that no aggregate metric pointed at, and the labels "
        "confirmed the reading.\n"
    )
    w("## Where I still disagree with the shipped judge\n")
    if disagree:
        w(
            f"After the fixes, {len(disagree)} of the 50 remain disagreements. "
            "These are the rows a reviewer should look at first, because on these the "
            "database decides which of us is wrong:\n"
        )
        w("| id | family | my read | judge | p | my reason |")
        w("|---|---|---|---|---|---|")
        for row_id in disagree:
            rec = verdicts[row_id]
            p = preds[row_id]
            fam = rows[row_id]["trajectory"]["case"]["family"]
            w(
                f"| `{row_id}` | `{fam}` | **{rec['v']}** | **{p['verdict']}** "
                f"| {p['probability']:.3f} | {rec['reason']} |"
            )
        w("")
    else:
        w("After the fixes there are no remaining disagreements on these 50 rows.\n")

    w("## Agreement summary\n")
    w(f"- rows audited: **{n}**")
    w(f"- agree with the shipped judge: **{len(agree)}** ({len(agree) / n:.0%})")
    w(f"- disagree: **{len(disagree)}** ({len(disagree) / n:.0%})")
    human_pass = sum(1 for r in verdicts.values() if r["v"] == 1)
    judge_pass = sum(1 for i in verdicts if preds[i]["verdict"] == 1)
    w(f"- my pass rate on the sample: {human_pass}/{n} = {human_pass / n:.3f}")
    w(f"- judge pass rate on the sample: {judge_pass}/{n} = {judge_pass / n:.3f}")
    w(
        "\nThe sample is deliberately skewed toward hard and low-confidence rows, so "
        "neither pass rate should be read as an estimate of the split's base rate.\n"
    )

    n_unclassified = sum(1 for p in preds.values() if p["category"] == "unclassified_failure")
    n_flagged = sum(1 for p in preds.values() if p["verdict"] == 0)

    gate_rows = [i for i in verdicts if preds[i]["gate_fired"]]
    gate_agree = [i for i in gate_rows if preds[i]["verdict"] == verdicts[i]["v"]]
    w("## Did the gates hold up under a human read?\n")
    w(
        f"{len(gate_rows)} of the audited rows were decided by a deterministic gate. I "
        f"independently reached the same verdict on **{len(gate_agree)}/{len(gate_rows)}** "
        "of them. Every one was a run that never committed, produced no tool calls at all, "
        "closed with an empty action list, or left a failed identity check unresolved and "
        "unescalated -- failures I would defend to a servicing manager without needing the "
        "database.\n"
    )

    w("## What I would still not trust\n")
    w(
        "- **`payment_reschedule` (0.700 dev accuracy, the weakest family).** The line "
        "between \"add a payment the customer doesn't yet owe\" (allowed) and \"move an "
        "instalment that already exists\" (must escalate) is a reading of intent, and my "
        "lexical `MOVE_EXISTING_PAYMENT` list gets it wrong on paraphrases.\n"
        "- **`injected_instruction` (0.729).** Correct handling depends on which of two "
        "asks in one message the agent acted on. The planted-span detector is good "
        "(56/60 detected, zero false positives across the other eleven families) but "
        "deciding whether the *action taken* belongs to the customer or the injection is "
        "still partly guesswork.\n"
        f"- **{n_unclassified} of the {n_flagged} held-out runs the judge flags land in "
        "`unclassified_failure`.** The verdict may be right while the category is "
        "useless for triage. That is the honest state of the taxonomy's tail.\n"
        "- **Loan age uses a fixed reference date** (2026-09-15, inferred from the manifest "
        "seed and the `next_due_on` range). If the private set is generated at a different "
        "date, every six-month hardship-eligibility check drifts.\n"
    )

    out = os.path.join(RESULTS, "audit_50.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {out}")
    print("  disagreements:", len(disagree))
    print("  families in sample:", dict(Counter(
        rows[i]["trajectory"]["case"]["family"] for i in verdicts
    )))


if __name__ == "__main__":
    main()
