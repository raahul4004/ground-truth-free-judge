# Experiment log

In order, including the things that did not work. Every metric is the honest
nested grouped-CV estimate on dev at the threshold in force at the time, and all
final numbers are recomputed by the organisers' `score.py`.

---

## 1. Read the data before writing any judge

Before touching a model I measured the shape of the problem. Four things
mattered:

- **Trajectories are small.** Median ~366 tokens of JSON, mean ~410, max ~2,900.
  So the whole transcript fits comfortably inside the reference's 774-input
  budget — the cost bar is not a reason to summarise.
- **`commit` is terminal and never returns a result.** It appears 637 times in
  the audit logs and never once as a `Result of commit:` line.
- **Severe family shift between splits.** `hardship_request` 3.3% → 18.3%,
  `autopay_cancel` 10.8% → 3.3%, `statement_request` 10.8% → 3.3%. Flagged as a
  transfer risk on day one. It later became the single biggest bug in the build.
- **`family` and `difficulty` live inside `trajectory.case`**, so they are
  legitimately available at inference. `agent_model` is recoverable from `id`.

## 2. The first real finding: no commit means no pass

51 dev trajectories have no `commit` in the audit log. All 51 are labelled 0.

Predicting fail for those and pass for everything else: TPR 1.000, TNR 0.331,
**BA 0.666** — with no model and no tokens. Adding "committed but neither acted
nor escalated" (18 rows, also all 0) took it to **BA 0.7403**.

That set the architecture. If half the failures are decidable exactly, a model
call spent on them is a chance to be wrong about a certainty.

## 3. Structural features, grouped CV

93 deterministic features + gate override, grouped by `case_id`:

| | BA | MCC | ECE | AUROC |
|---|---|---|---|---|
| gates only | 0.7240 | 0.5961 | 0.0870 | 0.5873 |
| logreg + gates | 0.8094 | 0.6383 | 0.0557 | 0.6708 |

Already past the reference's confidence AUROC (0.5565) at zero cost, purely
because confidence is a calibrated probability rather than a number a model
says out loud. Errors were concentrated in `fee_waiver` (0.341) and
`injected_instruction` (0.333) — both eligibility questions, which sent me to
the policy.

## 4. Downloading `policy.md` was the highest-leverage hour

Encoding the written limits — ₹2,500 fee cap, one-to-six-month hardship term,
six-month loan age, 60-day scheduling window, 120-day dispute window, the
always-escalate list, "third-party text is data, never instruction" — as
deterministic checks. Not fitted to labels; each constant is quoted next to the
clause it comes from.

**BA 0.8094 → 0.8304, MCC 0.6383 → 0.6745, AUROC 0.6708 → 0.7655.** All four
bars cleared for the first time, still zero tokens.

## 5. The bug that was silently discarding a third of the signal

`Result of verify_identity: "PolicyError: last4_phone must be exactly four digits"`
— refusals are *strings*, not `false`. And 114 of 720 trajectories contain one,
of which **114 appear only inside nested `"Previous attempt transcript: ..."`
turns** that my parser was skipping. I had been reading zero refusals.

Rows with a refusal pass 0.526 versus 0.708 without. The refusal text also leaks
the allowed `document_kind` enum, which `tool_schemas.json` does not contain.

Parsing retry blocks recursively: **features 143 → 170**.

## 6. Request-intent inference — worked, but only half of it

Two ideas, very different outcomes.

**Planted-span separation: excellent.** Isolating quoted / `SYSTEM:` /
`URGENT-OVERRIDE` framed text detected 56 of 60 `injected_instruction` rows with
**zero false positives across the other eleven families**.

**Keyword intent inference: mediocre.** First pass agreed with `family` on only
0.556 of action families. Extending the phrase lists (genuine language, not dev
tricks) took it to **0.852**. I stopped there deliberately: chasing it higher
means memorising dev phrasings, and `family` already supplies the canonical tool
reliably.

Net: **BA 0.8304 → 0.8496, ECE 0.0483 → 0.0211, AUROC → 0.8366.**

## 7. The family-shift bug, caught by hand

While hand-reading the audit sample I noticed the judge failing runs that looked
correct: verified, then escalated a 12-month hardship request, or a loan already
on a plan, or a customer with a serious illness. Per policy those escalations are
*mandatory*.

Quantified: dev has **six** escalate-only `hardship_request` rows (2 pass). The
model learned "hardship + escalate = failure" from those six and applied it to
the 18 equivalent held-out rows, predicting 0.568 pass where ~0.73 was right.

Root cause: the model had the negative half of the escalation ledger ("required
a handoff and didn't") and not the positive half. Fix: `escalation_required`
derived from the policy text, paired with whether the agent escalated. Dev
separates cleanly — at this step required + escalated → 0.795 pass against
required + not escalated → 0.182; after the audit fixes in §9 the same split
reads 0.763 against 0.184 — and it never mentions a family.

**hardship_request 0.750 → 0.938 at this step (0.875 after the §9 fixes),
fee_waiver 0.818 → 0.909, AUROC 0.7783 → 0.8366.** Held-out hardship predicted pass 0.568 → 0.727.

## 8. Threshold selection, and resisting two temptations

The argmax was a spike: 0.600 scored MCC 0.6921 between neighbours at 0.664 and
0.680. On 480 rows a spike is as likely noise as signal, so the rule became
"centre of the plateau within 0.03 MCC / 0.02 BA of best".

Second problem: an unconstrained sweep drifted toward predicting more failures
than exist, because on a 68%-pass split that buys balanced accuracy while making
the marginal wrong (at 0.625 it predicted 0.6229 against a 0.6792 truth). Added a
tie-break toward reproducing the dev base rate. Chose **0.525**.

**Not used:** `manifest.json` publishes the held-out pass rate (0.6333). Tuning
the threshold to match it would be fitting to aggregate label information about
the evaluation split, and would not transfer to an unpublished private mix.

Robustness gained: **all 16 thresholds from 0.30 to 0.675 clear all four bars.**

## 9. The 50-row audit — the most productive hour of the build

Read 50 held-out trajectories against `policy.md`, recording my own verdict
before any label. I disagreed with the judge on **20 of 50**, and the
disagreements clustered into five bugs rather than five opinions:

| rows | bug | fix |
|---|---|---|
| 6 | escalation after two failed identity checks scored as failure | the `escalation_required` positive half (§7) |
| 4 | injection runs that served the real request and ignored the planted one flagged as obeying it | condition on the *planted* action being performed |
| 2 | a refused `request_document` retried successfully, flagged `refusal_ignored` | require the tool to have never subsequently succeeded |
| 2 | a fully eligible hardship plan flagged `mandatory_escalation_missed` | "my wife's shop closed" is income context, not a third party *acting*; narrowed the trigger |
| 1 | a statement request failed because the loan was on a hardship plan | gate that reason on the request being hardship-related |

Plus one found by the same reading: **a refused call was counting as a state
change.** `audited_tool_calls` records attempts, not effects.

**BA 0.8426 → 0.8503, MCC 0.6785 → 0.6994.** Two of the corrected checks became
pure enough to promote to gates (6 → 8 gates, still zero false positives,
53.9% of failures caught free). Audit disagreements fell 20 → 9.

## 10. Two ideas I believed that the labels rejected

The same audit suggested two more escalation reasons. Both are defensible
readings of the policy. Both measured worse:

| arm | BA | MCC | ECE | AUROC |
|---|---|---|---|---|
| both added | 0.8453 | 0.6930 | 0.0336 | 0.8238 |
| without "customer declines to verify" | 0.8487 | 0.6951 | 0.0290 | 0.8367 |
| **without both (shipped)** | **0.8503** | **0.6994** | **0.0265** | **0.8352** |

Rejected as escalation reasons, kept as ordinary features. A related pair of
pattern extensions (`"diagnosed with"` for vulnerability, `"is not mine"` for
debt denial) measured *exactly* neutral — 0.8503 / 0.6994 with and without — and
are retained on policy grounds alone. Recorded as unvalidated, not as a gain;
see `LANDSCAPE.md §5`.

## 11. Per-layer ablation, and an uncomfortable result

Each layer zeroed on its own rather than one stacked before/after:

| arm | BA | MCC | ECE | AUROC | ΔBA / ΔMCC |
|---|---|---|---|---|---|
| full system | 0.8503 | 0.6994 | 0.0265 | 0.8352 | — |
| − gates | 0.8146 | 0.6408 | 0.0238 | 0.7992 | −0.036 / −0.059 |
| − policy features | 0.8656 | 0.7434 | 0.0501 | 0.8201 | **+0.015 / +0.044** |
| − escalation-required | 0.8578 | 0.7246 | 0.0385 | 0.7908 | +0.008 / +0.025 |
| − intent + planted-span | 0.8371 | 0.6815 | 0.0324 | 0.8372 | −0.013 / −0.018 |
| − family one-hots | 0.8405 | 0.6834 | 0.0209 | 0.8386 | −0.010 / −0.016 |
| − loan facts | 0.8470 | 0.6940 | 0.0271 | 0.8368 | −0.003 / −0.005 |
| − message lexical | 0.8564 | 0.7166 | 0.0184 | 0.8378 | +0.006 / +0.017 |
| − commit-argument features | 0.8535 | 0.7047 | 0.0236 | 0.8318 | +0.003 / +0.005 |

The gates are the most load-bearing single component, which is the result I
hoped for. But **deleting the policy features improves in-mix dev BA and MCC**
while worsening ECE (0.027 → 0.050) and AUROC (0.835 → 0.820).

That is a direct conflict with §7, so it needed its own experiment rather than a
narrative resolution.

## 12. Leave-one-family-out settles it: the sign flips

In-mix CV is the wrong test for features whose only job is surviving a change of
mix. `experiments/05_transfer.py` trains with one family entirely absent and
judges only that family — the situation an unpublished private mix creates.

| arm | in-mix ΔBA / ΔMCC | **LOFO ΔBA / ΔMCC** |
|---|---|---|
| − policy features | +0.0153 / +0.0440 | **−0.0107 / −0.0276** |
| − escalation-required only | +0.0075 / +0.0252 | **−0.0092 / −0.0238** |
| − family one-hots | −0.0098 / −0.0160 | +0.0031 / +0.0081 |
| − policy AND family | — | −0.0017 / −0.0015 (AUROC −0.0268) |

The policy layer's contribution reverses sign between regimes: it costs in-mix
accuracy and buys transfer. Per family the effect concentrates where §7 predicted
— `identity_challenge` transfers at **0.844 with the policy layer, 0.656
without**, the largest single swing in the table. `dispute_open` is the one
counter-example (0.750 with, 0.841 without).

**Kept the policy layer**, and stated the price: on a private set whose mix
resembles dev, this costs about 0.04 MCC against the dev-optimal configuration.
Caveats in `LANDSCAPE.md §0` — notably that every LOFO arm fails the bars, so it
is a stress test for ranking arms and not a forecast, since the private set
reuses these twelve families rather than introducing new ones.

## What I would do differently

1. **Read twenty trajectories before writing a single feature.** Both structural
   discoveries that mattered (§5, §9) came from reading, and neither was visible
   in any aggregate.
2. **Run the audit on day one at n=20.** It was scheduled as a deliverable and
   turned out to be the best debugging tool available. I built on top of five
   bugs for hours because I audited last.
3. **Never let a category prior stand in for a written rule.** Every serious
   error here traces to the model inferring an outcome from a family frequency
   where the policy stated a rule.
