# Judge Without Ground Truth

A judge for Harbour loan-servicing trajectories. It reads a run — the customer
message, the tool calls, and what each tool returned — and emits a verdict, a
calibrated confidence, and an operational failure category. It never sees the
database.

```
judge(trajectory, tool_schemas, case) -> {verdict, confidence, category}
```

## Result

Honest development estimate, from **nested grouped cross-validation** and
recomputed by the organisers' own `score.py`:

| Bar | Target | This judge | Reference judge |
|---|---|---|---|
| Balanced accuracy | ≥ 0.79 | **0.8503** | 0.7728 dev / 0.7658 held-out |
| Matthews correlation | ≥ 0.65 | **0.6994** | 0.6022 / 0.5885 |
| ECE | ≤ 0.10 | **0.0265** | 0.0621 / 0.0810 |
| Confidence AUROC | ≥ 0.65 | **0.8352** | 0.6004 / **0.5565** |
| Cost per trajectory | ≤ 774 in / 500 out tokens | **0 / 0** | 774 / 500 |

All four bars clear, and the confidence AUROC — the bar the organisers publish
as unsolved, and which their own reference misses at 0.5565 — clears by 0.185.

**It does this with zero model calls.** No API key, no network, no GPU.
240 trajectories in 5.5 seconds, about 23 ms each.

Grouping matters: dev and held-out are disjoint *by case*, and four
trajectories share every case, so a random row split leaks. Every number above
comes from folds split on `case_id`, with the calibrator refitted inside each
fold — see `scripts/evaluate_dev.py` for why nesting is not optional here.

## How it works

Three layers, cheapest first.

**1 — Deterministic gates (0 tokens, decides 19.6% of rows).**
Eight checks that are *exact* on dev: they fire on 83 of 480 rows with a
**0.0000** pass rate. A run with no `commit` never passed, not once in 51
cases. Nor did one that committed without acting or escalating, or emitted an
empty turn, or claimed an action its audit log never recorded. A check is only
allowed to override the model if it has never once condemned a passing run;
purity is re-verified on every run and printed as `passing runs among gated
rows (must be 0)`.

**2 — Calibrated structural model (0 tokens, decides the rest).**
214 deterministic features over the transcript, fed to a five-fold
gradient-boosted ensemble, Platt-calibrated. The features that carry the weight
are policy-derived: whether identity preceded money movement, whether a handoff
was *mandatory* and whether it happened, whether an action exceeded a published
limit, whether a refusal was recovered from or ignored.

Confidence is `max(p, 1-p)` on the calibrated probability. That single choice is
what clears the AUROC bar — a probability is continuous and ordered by evidence,
where a number a language model says out loud clumps onto a few habitual values
and orders almost nothing.

**3 — LLM adjudication (optional, `--use-llm`, off by default).**
~17% of rows land in a middling band that carries ~48% of the residual error,
concentrated where correct handling turns on which of two asks in a message the
agent acted on. A budget-class model is consulted on those rows only, and it is
advisory: it nudges a logit, it cannot overturn a gate. Every call is metered to
a token ledger. **Not validated against a live endpoint** — no key was
available here, so its metrics are `null` rather than estimated.

## Two things in the data that most of the signal hides behind

**Refusals live in the retry blocks.** 114 of 720 trajectories contain a refused
tool call, and *all 114* appear only inside a nested
`"Previous attempt transcript: ..."` user turn. A parser reading top-level turns
sees zero refusals. Rows with a refusal pass 0.526 of the time versus 0.708
without. The refusal text also leaks the allowed `document_kind` enum, which
`tool_schemas.json` does not contain.

**A refused call is not an action.** `audited_tool_calls` records attempts, not
effects. Counting membership as "state changed" makes a refused
`request_document` look like an unrequested action — and makes a run that
*recovered* from the refusal look like one that ignored it.

Both were found by reading trajectories, not by staring at metrics.

## Run it

```bash
pip install -r requirements.txt
./scripts/reproduce.sh              # full pipeline: train, evaluate, predict, score
```

Or the graded path directly:

```bash
python -m judge.cli train   --dev data/dev.jsonl --out artifacts/structural.pkl
python -m judge.cli predict --input data/heldout.jsonl --out results/heldout_predictions.jsonl
```

`predict` drops any `label` field before the judge sees a row, so a labelled
file cannot leak an answer. Add `--use-llm` (and an `OPENAI_API_KEY`) for
layer 3; `--ledger results/ledger.jsonl` records per-call tokens and cost.

`data/` holds the organisers' fixtures and is gitignored — point `--dev` and
`--input` at your own copy of `references/OP-04/`.

## Layout

| Path | What it is |
|---|---|
| `judge/features.py` | Transcript parsing (incl. nested retries, refusals) + structural features |
| `judge/policy.py` | Deterministic checks against `policy.md`, with each limit quoted |
| `judge/intent.py` | Request-intent inference and planted-span separation |
| `judge/model.py` | Fold ensemble, Platt calibration, gate override |
| `judge/taxonomy.py` | 17 operational failure categories with severity and owner |
| `judge/llm.py` | Optional budget-class adjudicator + token ledger |
| `judge/cli.py` / `judge/api.py` | CLI and the `judge()` interface |
| `scripts/evaluate_dev.py` | Nested grouped CV; invokes the official `score.py` |
| `experiments/01–05` | Signal analysis, gate-purity audit, threshold selection, per-layer ablation, leave-one-family-out transfer |
| `scripts/preflight.py` | Pre-submission checks against SUBMISSION_SCHEMA.md (56 assertions) |
| `results/audit_50.md` | 50 held-out decisions read by hand — **start here** |
| `DECISIONS.md` | Every fork, and what would make the other branch right |
| `LANDSCAPE.md` | What I do not trust, and the failure modes I would expect |

## Where it is weak

- **`payment_reschedule`, 0.700 dev accuracy.** "Add a payment" (allowed) vs
  "move the instalment you already have" (must escalate) is a reading of
  intent, and lexical matching gets paraphrases wrong.
- **`injected_instruction`, 0.729.** The planted-span detector is strong
  (56/60, zero false positives across the other eleven families) but attributing
  the *action* to the customer or the injection is still partly guesswork.
- **Held-out marginal.** The judge predicts a 0.7167 pass rate against the
  published 0.6333. I did not tune to that number — it is aggregate label
  information about the evaluation split — and report the gap instead.
  `LANDSCAPE.md` works out what it bounds.
- **18 of the 68 held-out runs it flags land in `unclassified_failure`.**
  The verdict may be right while the category is useless for triage.

`results/audit_50.md` is the honest centre of this submission: reading 50
trajectories by hand found five bugs no aggregate metric pointed at, and fixing
them moved dev from BA 0.8426 / MCC 0.6785 to BA 0.8503 / MCC 0.6994.

## Compliance

Harbour is never imported, executed or vendored, and no backend state is
reconstructed or replayed. `references/OP-01/harbour/` was never downloaded —
only `policy.md` (a document, and the one the reference judge is described as
using) and `references/OP-04/`. Budget-class models only. All fixtures are the
organisers' released files, unmodified.
