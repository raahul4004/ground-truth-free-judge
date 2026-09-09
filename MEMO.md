# MEMO — to the product owner

**Subject:** the daily quality number, what it costs, and when to stop trusting it

---

## The number

Your agent's runs can be scored automatically, every night, with no labels and
no frontier model rereading transcripts. On the data we have, the judge agrees
with the database **86%** of the time, and its balanced accuracy — which weights
catching failures as heavily as confirming successes — is **0.85**.

Report it as a **daily pass rate with a ±4 point band**. If yesterday reads 71%
and today reads 69%, nothing has happened. A move of more than about 5 points is
real and worth someone's morning.

## What it costs

Nothing per run. The judge makes no model calls. It reads the transcript your
agent already produces and scores 240 runs in about five seconds on an ordinary
CPU. At your volumes the marginal cost of the daily number is the machine it
runs on.

That was a design choice, not luck. Roughly a fifth of runs can be settled by
checks that cannot be wrong — a run that never closed, a run that closed having
done nothing, a run whose summary claims an action its own audit log never
recorded. Paying a model to re-derive those is paying for a chance to get them
wrong.

There is an optional layer that spends a small model on the genuinely ambiguous
runs. It is written but not yet switched on, because the number above clears the
bar without it. If we turn it on, budget cents per thousand runs, not dollars.

## How much to trust it

**Trust the confidence.** This is the part I would defend hardest. When the
judge says it is 95% sure, it is right about 95% of the time; when it says 55%,
it is close to a coin flip. That means your team can sort a day's flagged runs by
confidence and read from the bottom up, and stop when the returns dry up. A
judge whose confidence means nothing forces you to read everything, which is the
work you wanted removed.

Concretely: the low-confidence tail is about a sixth of runs and holds about half
the judge's mistakes. Reading that sixth catches most of what the judge got wrong.

**Do not trust it as an absolute pass rate.** It currently reads about 8 points
higher than truth on the evaluation split — it is more willing to call a run
good than the database is. Use it to track *change over time*, and treat the
level as approximate until we have measured it against a labelled week of real
traffic.

## When to disregard it entirely

Four situations, in order of how likely they are:

1. **Your case mix shifts.** This is the real fragility, and it has already
   bitten once. An early build learned "hardship request that gets handed off =
   failure" from six examples, then applied it to a slice where handing off was
   the *correct* answer, and got that whole family wrong. It is fixed — the judge
   now reasons from the policy rather than from how often a category passed
   before — but the lesson stands: if you launch a new request type, or the
   volume of an existing one moves sharply, the number is unvalidated until
   someone reads fifty runs of it.
2. **The policy changes.** The fee cap, the hardship term limits, the scheduling
   window and the always-escalate list are written into the judge as numbers.
   Change the policy and the judge is silently scoring against the old one. This
   needs to be a line item in any policy change.
3. **You change the agent's model.** Two agent models are covered, and they fail
   differently. A third is an unknown.
4. **The date drifts.** Loan-age eligibility is computed against a fixed
   reference date. Left alone for a year, that check rots.

## The first three failure categories worth a dedicated person

Ranked by what they cost you, not by how often they happen.

**1. Money moved without an identity check** — *give this to controls.* The
smallest bucket and the only one that is a straightforward breach of the written
policy rather than a quality problem. Every instance is a fee forgiven or an
obligation changed on an unverified contact. These should be zero, they are not,
and each one is individually indefensible in an audit. Start here even though
the volume is low.

**2. Runs that never closed** — *give this to reliability.* The largest single
failure bucket: about a tenth of all runs end without a `commit`, sometimes with
no tool calls at all, sometimes with the model emitting an empty turn. The
customer was not served and no outcome was recorded. This looks like an
infrastructure or retry fault rather than a judgement fault, which makes it the
cheapest thing on this list to fix and probably the biggest single win
available.

**3. A handoff the policy required that did not happen** — *give this to
compliance.* Bereavement, serious illness, insolvency, a third party acting for
the customer, suspected fraud, a legal or regulator threat. The policy says
these always reach a person; sometimes they do not. Lower volume than (2), far
higher consequence, and the one a regulator would ask about.

One caveat on the taxonomy: **18 of the 68 runs it flags** land in
`unclassified_failure` — about a quarter. The judge believes they failed and
cannot say why in a way you could staff against. That tail is honest, and it is
where I would spend the next week of effort.

## What I would fund next

A **labelled week of production traffic** — a few hundred real runs with the
backend diff attached. Everything above is measured on 180 synthetic cases. That
one dataset would convert the pass rate from a trend line into a number you can
put in front of a regulator, and it would tell us whether the 8-point optimism
is a property of the judge or of the synthetic set.

Second, and cheaper: switch on the confidence-triage queue. The judge already
tells you which of its verdicts to distrust. Nobody is reading that signal yet.
