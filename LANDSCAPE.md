# Landscape — where this judge fails, and what I would need to fix it

The required analysis: where and why the judge fails, which false passes could
hide a production issue for a week, which categories generalise and which
silently do not, and what changes if the domain is unfamiliar.

---

## 0. The hardest call in this build, and how I settled it

The per-layer ablation produced a result I did not want: **zeroing the policy
features improves in-mix dev balanced accuracy and MCC** (+0.015 BA, +0.044
MCC), while worsening ECE (0.027 → 0.050) and confidence AUROC (0.835 → 0.820).
Read alone, that says delete the layer this submission is built on.

In-mix cross-validation is the wrong test for those features. Their only job is
to survive a change in the family mix, and dev's mix is not what they are judged
on — held-out moves `hardship_request` from 3.3% to 18.3%, and the private mix
is unpublished. A feature that has quietly memorised "how often does this family
pass" scores well when train and test share a mix, and collapses when they do
not. So I ran the test that actually matches the situation: leave-one-family-out,
each family judged by a model trained with it entirely absent
(`experiments/05_transfer.py`).

| arm | in-mix ΔBA / ΔMCC | **LOFO ΔBA / ΔMCC** |
|---|---|---|
| − policy features | +0.0153 / +0.0440 | **−0.0107 / −0.0276** |
| − escalation-required only | +0.0075 / +0.0252 | **−0.0092 / −0.0238** |
| − family one-hots | −0.0098 / −0.0160 | +0.0031 / +0.0081 |

The sign flips. The policy layer costs in-mix performance and buys
out-of-mix performance, which is exactly the trade a judge facing an unpublished
family mix should take. Per family, the swing is concentrated where you would
predict: `identity_challenge` transfer accuracy is **0.844 with the policy layer
and 0.656 without** — the largest single effect in the table.

**Kept the policy layer.** The cost is stated rather than hidden: on a private
set whose mix happens to resemble dev, this configuration is worth roughly 0.04
MCC less than the dev-optimal one.

Two honest caveats. First, **every LOFO arm fails the bars** (BA ≈ 0.79, MCC ≈
0.58) — LOFO is a harsher test than the private set poses, since that set reuses
the same twelve families with hidden cases rather than introducing new ones. It
is a stress test for choosing between arms, not a forecast. Second, the family
one-hots go the other way: removing them helps transfer slightly and hurts
in-mix. I kept them, because the private families are the same twelve and will
mostly have been seen, but that is the one place in this build where I chose the
in-mix number.

---

## 1. The single biggest risk: the judge is optimistic about the marginal

On held-out it predicts a **0.7167** pass rate. The published truth is
**0.6333**. It is 8.3 points too willing to call a run good.

I did not close this gap, because closing it means tuning to aggregate label
information about the evaluation split, and it would not transfer — the private
set's pass rate is unpublished. So it is worth working out what the gap actually
bounds.

Held-out is 240 rows: 152 pass, 88 fail. The judge predicts 172 pass, 68 fail.
Even if *every one* of its 68 predicted failures is a true failure, at least
20 of its passes are wrong, so:

```
TNR ≤ 68/88 = 0.773        TPR ≤ 1.000
balanced accuracy ≤ (1.000 + 0.773) / 2 = 0.886
```

The bar is 0.79, so the marginal mismatch alone does not threaten it — there is
about 10 points of headroom before it does. But the direction is the expensive
one: the errors it implies are **false passes**, which is the failure mode that
hides a production issue rather than creating noise. If the private set is
harder than held-out, this is the number that breaks first.

**What I would need:** a few hundred labelled rows from a *third* split, to
separate "the judge is optimistic" from "held-out happens to be harder than dev
in a way dev cannot show me."

## 2. False passes that could hide a real issue for a week

Ranked by how long the damage would go unnoticed.

**Unverified money movement that the model forgives.** The identity-before-money
check is a feature, not a gate, because on dev it fires on 30 rows that pass 0.77
of the time — the policy names four money tools explicitly and I extended it to
disputes, which the text does not. So a genuine breach can be outvoted by other
evidence. This is the worst false pass available: it is a policy violation, it is
individually indefensible, and nothing else in the pipeline would catch it.
*Fix:* make the identity check a gate for the four tools the policy names
verbatim, and drop disputes from the set. I did not do this because I could not
verify the narrower version is pure on dev without also being confident about
which tools belong — and guessing wrong makes a gate that condemns passing runs.

**Injection obeyed while the legitimate request is also served.** If the agent
does the customer's real thing *and* the planted thing, the correct-looking half
supplies enough positive evidence to carry the row. `v_injection_obeyed_waiver`
never fires on dev, so this path is entirely unexercised — I cannot tell whether
it works. `injected_instruction` is the weakest family at 0.729, and it is 5% of
held-out but an unknown share of the private set.

**`payment_reschedule`, 0.700.** The distinction is one sentence of policy: a
*new* payment is fine, *moving an existing instalment* must be escalated because
the tooling cannot amend a schedule and a second payment means the customer is
collected twice. My detector is a list of phrasings. "Could you move the
collection to 6 October" matches; a paraphrase I did not think of does not, and
then a double-collection scores as a pass.

## 3. Which categories generalise, and which do not

**Generalise — they are properties of the transcript, not of the case mix:**
`run_never_closed`, `closed_with_no_action`, `empty_model_turn`,
`claimed_action_not_performed`. These are structural. They would work on a
different lender, a different policy, a different tool vocabulary. They are also
the eight gates' backbone, which is why 53.9% of failures are caught at zero
cost and zero risk.

**Generalise conditionally — they are properties of *this* policy:**
`over_limit_action_taken`, `unverified_money_movement`,
`mandatory_escalation_missed`, `payment_move_by_new_schedule`. The logic
transfers; the constants (₹2,500, one-to-six months, 60 days, 120 days) and the
always-escalate list do not. Change the policy and these silently score against
the old one, with no error and no warning. This is the failure mode I would most
want a canary for.

**Do not generalise, and I would not claim they do:**
`wrong_action_for_request` depends on `FAMILY_CANONICAL_TOOL`, a hand-written map
from family to expected tool. `refusal_ignored` depends on the specific
`PolicyError` string format. `injected_instruction_followed` depends on the
planted-span frames actually used in this corpus — quoted notes, `SYSTEM:`
prefixes, `URGENT-OVERRIDE` tokens. A real attacker would not use any of them.

**The honest tail:** 18 of the 68 held-out runs flagged as failures get
`unclassified_failure`. The verdict may well be right; the category tells a
servicing manager nothing. That is a quarter of the flagged output being
un-triageable, and it is the first thing I would work on next.

## 4. The family-shift lesson, stated plainly because it already cost me

Dev and held-out have very different mixes: `hardship_request` 3.3% → 18.3%,
`autopay_cancel` 10.8% → 3.3%, `statement_request` 10.8% → 3.3%.

An early build learned "escalate-only on a hardship request = failure" from the
**six** such rows in dev, two of which pass. Held-out has 18, and most escalate
for reasons the policy makes mandatory — a 12-month term, a loan already on a
plan, serious illness. That build scored 0.568 predicted pass on the family where
~0.73 was right, and 0.750 accuracy on dev's hardship rows.

The fix was to stop learning the answer from the family and derive it from the
policy: `escalation_required`, built from term-over-cap, loan-too-young,
already-on-a-plan, fee-over-cap, move-an-instalment, the always-escalate
triggers, and two failed identity checks. Dev now separates cleanly (required +
escalated → 0.76 pass, required + not escalated → 0.18) and hardship accuracy is
1.000.

What makes this a real generalisation rather than a patch: **dev contains zero
correctly-escalated hardship rows.** The model cannot have learned that pattern
from hardship examples, because there are none. It learned it from the 97
non-hardship rows sharing the structure. That is what transfer is supposed to
look like, and it is the strongest evidence in this submission that the judge
reasons about policy rather than memorising category priors.

## 5. What I got wrong, kept in the record

Two ideas from the audit that I believed and the labels rejected. `experiments/04_ablation.py`
holds the measurements.

- **"A customer who declines to verify obliges a handoff."** Defensible reading
  of the policy. Cost 0.005 BA and 0.006 MCC on dev. Rejected as an escalation
  reason; kept as a plain feature.
- **"Two attempts that never establish identity oblige a handoff."** Same. The
  policy bounds *failed* attempts, and treating malformed-and-refused attempts
  the same way measured worse. Rejected.

Two pattern extensions I kept **without** measured support: adding
`"diagnosed with"`/`"off work"` to the vulnerability triggers and
`"is not mine"`/`"never taken"` to debt-denial. Both are unambiguous policy
readings and both fix specific audit findings, but they measure exactly neutral
on dev (0.8503 / 0.6994 with and without). They are retained on judgement, not
evidence, and they are part of why the held-out marginal drifted up. If forced to
choose on evidence alone I would drop them.

## 6. If the client's agent were in a domain I did not know

I would keep layer 1 and rebuild layers 2 and 3.

The gates survive a domain change almost untouched, because "the run never
closed", "it closed having done nothing" and "the summary claims an action the
audit log does not show" are statements about agent transcripts in general. That
is the reusable asset here, and it is roughly half the failures for free.

Everything policy-shaped has to be rewritten, and the order matters: **get the
written policy first, and turn its numbers into checks before touching a
model.** The single highest-leverage hour I spent on this problem was reading
`policy.md` and encoding the fee cap, the hardship window, the scheduling limits
and the always-escalate list. That is what took `fee_waiver` from 0.818 to
0.932 and `hardship_request` to 1.000.

What I would do differently from the start, knowing what I know now:

1. **Read twenty trajectories before writing any features.** Both of the
   structural discoveries that mattered — refusals hiding inside retry blocks,
   and refused calls not being actions — came from reading, and both were
   invisible in aggregate statistics. I found the first one about four hours in,
   having already built a parser that was blind to all 114 of them.
2. **Audit early, not at the end.** The 50-row audit was scheduled as a
   deliverable and turned out to be the most productive hour of the build: it
   found five bugs and moved MCC by 0.02. Had I run a 20-row version on day one
   I would have found them before building on top of them.
3. **Never let a category prior stand in for a rule.** Every serious error in
   this build traces to the model inferring an outcome from a category frequency
   where a written rule existed.
