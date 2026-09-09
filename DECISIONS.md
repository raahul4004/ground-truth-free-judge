# Decisions

Each entry is a fork in the road, what I chose, and what would have to be true
for the other branch to have been right.

---

## 1. Deterministic evidence first, a model call only where it can still change the answer

**Chose:** a layered judge. Eight deterministic gates decide the certain
failures, a calibrated gradient-boosted model over 214 transcript features
handles the rest, and an optional budget-class LLM is consulted only for rows
whose probability lands in a middling band.

**Why.** The brief lists "deterministic checks over tool-call sequences" as a
legitimate architecture, and the data rewards it heavily. Every trajectory
without a `commit` fails — 51/51 on dev, no exceptions. So do runs that commit
without acting or escalating, runs with an empty model turn, runs whose closing
summary claims an action the audit log never saw. Those eight checks cover
**83 of 480 dev rows with a 0.0000 pass rate**: over half of all failures,
settled exactly, at zero tokens.

Spending a model call on those rows would be paying to re-derive a certainty,
and it would introduce a way to get them wrong.

**The other branch.** A single rubric-driven LLM call per trajectory is what the
reference judge does, and it reaches 0.7658 balanced accuracy on held-out with
confidence AUROC 0.5565. It would have been right to prefer it if the
deterministic signal had been weak — but the transcript turns out to contain
most of the answer, and a prompt cannot be made to *guarantee* that a
never-committed run is scored as a failure.

## 2. Confidence is a calibrated probability, not a number the model says out loud

**Chose:** `confidence = max(p, 1-p)` where `p` is a Platt-calibrated
probability from a fold ensemble.

**Why.** This is the bar the organisers call unsolved: their judge reaches
ECE 0.0213 but confidence AUROC **0.5563**, "much better at knowing whether it
is right than when it is right." That failure is structural, not a tuning
problem. Asking a language model to emit a confidence gets you a number drawn
from a handful of habitual values, which orders almost nothing — and the
scorer's AUROC counts ties as half a win, so a clumped distribution lands near
0.5 by construction.

A probability from a fitted model is continuous, monotone in the evidence, and
ordered by it. It reaches **0.8238–0.8352** on the honest nested-CV estimate.
Nothing clever happened here; the metric simply wants a real probability, so I
gave it one.

**Consequence worth stating:** this is why the whole design leans on a trained
model rather than a prompt. The verdict bars are reachable either way. The
confidence bar is the one that dictated the architecture.

## 3. Platt scaling, not isotonic regression

**Chose:** a sigmoid fit on out-of-fold scores.

**Why.** Isotonic regression is the usual reflex for calibrating tree
ensembles and it is the wrong choice *for this scorer*. It is piecewise
constant, so it maps many distinct scores onto identical values, and each tie
scores exactly half a win in the AUROC the design exists to clear. Platt
scaling is strictly monotone: it fixes the calibration without touching the
ordering.

**The other branch.** If ECE had been the binding constraint, isotonic's
tighter fit would win. ECE was never close (0.0265 against a 0.10 ceiling), so
there was nothing to buy and a ranking to lose.

## 4. `family` is used to interpret a request, never as a prior on the outcome

**Chose:** family one-hots stay in the vector, but the features that carry the
weight are policy-derived and family-independent.

**Why.** The held-out family mix is drastically different from dev:
`hardship_request` 3.3% → 18.3%, `autopay_cancel` 10.8% → 3.3%,
`statement_request` 10.8% → 3.3%. Any judge leaning on "how often does this
family pass" transfers badly, and the private set's mix is unpublished.

This is not hypothetical — it bit. Dev has exactly **6** escalate-only
`hardship_request` rows, of which 2 pass. An earlier build learned
"hardship + escalate = failure" from those six and then judged the 18
equivalent held-out rows the same way, when most of those escalate for reasons
the policy makes mandatory (a 12-month term, a loan already on a plan, serious
illness). Held-out predicted pass on that family was 0.568 when it should have
been near 0.73.

**Fix that generalises:** `escalation_required`, computed from the policy text —
term over cap, loan too young, already on a plan, fee over cap, a move-an-
instalment request, a vulnerability or insolvency or fraud or legal trigger,
two failed identity checks. Paired with whether the agent escalated, it splits
dev cleanly (required + escalated → 0.76 pass; required + not escalated →
0.18) and it says nothing about which family the row came from. Dev contains
**zero** correctly-escalated hardship rows, so the model can only learn this
from the 97 non-hardship rows that share the pattern — which is exactly what
transfer should look like.

## 5. Refusals are read out of the retry blocks

**Chose:** parse the nested `"Previous attempt transcript: ..."` user turn
recursively and treat `"PolicyError: ..."` string results as refusals.

**Why.** 114 of 720 trajectories contain a refused tool call, and **all 114 of
them hide it inside a previous-attempt transcript**. A parser that reads only
top-level turns sees zero refusals — I shipped one for the first few hours and
it was silently blind to the entire signal. Rows with a refusal pass 0.526 of
the time against 0.708 without.

The refusal text also leaks a fact the schemas do not contain: the allowed
`document_kind` enum, recoverable from `"allowed: income_proof,
bank_statement, id_proof, address_proof, medical_certificate,
employment_letter"`. `tool_schemas.json` types `kind` as a bare string.

## 6. A refused call is not an action

**Chose:** state-change features count only tools that were not refused on
every observation.

**Why.** Found by reading 50 trajectories, not by looking at a metric. A
`request_document` refused for a bad `kind` was being counted as a state
change, which made a run that *recovered* — retried with `address_proof`, got
`dc_0026`, committed — look like both an unrequested action and an ignored
refusal. Two of the audit's disagreements were exactly this.

## 7. A check becomes a hard gate only if it is exact, and purity is re-verified every run

**Chose:** promotion rule — a check may override the model only if it fires on
at least 5 dev rows and has a **0.0000** pass rate among them. Everything else
is a feature and the model decides its weight.

**Why.** A gate that is wrong is worse than no gate, because it converts an
uncertain row into a confident error, and a confident error is the one that
damages both MCC and the confidence AUROC. `scripts/reproduce.sh` reruns
`experiments/02_policy_purity.py` and `scripts/evaluate_dev.py` prints
"passing runs among gated rows (must be 0)" on every run, so a regression here
is loud.

Two checks were promoted only *after* the audit fixed their false positives
(`mandatory_escalation_missed`, `refused_then_no_escalation`) — they were
impure because they were buggy, not because the idea was wrong.

## 8. Threshold selected on a plateau, with a calibration tie-break

**Chose:** 0.525, from the centre of the region within 0.03 MCC and 0.02 BA of
the best, tie-broken toward the threshold whose predicted pass rate is closest
to the dev base rate.

**Why.** The argmax was a spike — 0.600 scored MCC 0.6921 between neighbours at
0.664 and 0.680 — and on 480 rows a spike is as likely to be noise as signal.
Worse, an unconstrained sweep drifts toward predicting more failures than exist,
because on a 68%-pass split that buys balanced accuracy while making the
marginal wrong; at 0.625 the judge predicted a 0.6229 pass rate against a
0.6792 truth. The calibration tie-break removes that drift on dev-only
information.

**Robustness this bought:** every threshold from 0.425 to 0.75 clears all four
bars. The result does not depend on the number.

## 9. The published held-out pass rate was not used to tune anything

`manifest.json` publishes held-out pass rate 0.6333. It would have been easy to
nudge the threshold until the predicted marginal matched.

**Chose not to.** That is aggregate label information about the evaluation
split, and it would not transfer — the private set's rate is unpublished. The
threshold comes from dev alone. The judge predicts 0.6917 on held-out against
that published 0.6333, and I am reporting the 5.8-point gap as a limitation
rather than closing it by fitting to the answer. See `LANDSCAPE.md`.

## 10. The LLM layer is built, metered, and off by default

**Chose:** ship the escalation layer complete — prompt, OpenAI-compatible
client, retry handling, per-call token ledger — with `--use-llm` opt-in, and
report the primary result as the zero-token configuration.

**Why.** The structural judge clears all four bars without a single model call,
so the headline result should not be contingent on an API key, a provider
outage or a snapshot retirement. But ~17% of rows sit in a band carrying ~48%
of the residual error, concentrated in `injected_instruction` and
`payment_reschedule`, where correct handling depends on which of two asks in a
message the agent acted on. That is a reading of intent, and a small model is
the right tool for it.

Routing rather than blanket-calling is what keeps it affordable: the reference
spends 774 input / 500 output tokens on *every* trajectory, and a 17% call rate
on a ~400-token median transcript is an order of magnitude under that. The
layer is advisory — it nudges a logit, it cannot overturn a gate — so a
confidently wrong call is damped instead of decisive.

**Not yet measured against a live endpoint.** No key was available in this
environment. The code path is untested end-to-end and is declared `null` in
`submission.yaml` rather than estimated. Honest state: written, not validated.

## 11. Harbour is never imported, and was never even downloaded

The brief forbids importing, vendoring or replaying Harbour, and says automated
checks reject submissions whose judge touches the harness. Beyond not importing
it, `references/OP-01/harbour/` was never fetched — only `policy.md` (a
document, and the same one the reference judge is described as using) and
`references/OP-04/`. The repo was not cloned; individual files were downloaded.

The judge reads transcripts. It never reconstructs backend state, never infers a
schema to replay against, and holds no simulator. The one place this shows as a
deliberate limitation: fee-cap checks read the amount from the customer's
message, because the applied-fee row is not in the trajectory and inventing it
would be exactly the reconstruction the brief rules out.
