# Signature extension — X2: Proposal experiments

Two proposal strategies run as a controlled A/B test in GigSim, judged by
whether a simulated client actually replied, with a significance test on the
difference.

**Run it:** `python -m evals.runners.proposal_experiment --limit 40`
**Results:** `evals/results/proposal_experiment.json` — **not yet produced.**
Both arms need a live model and the free-tier quota was exhausted during
evaluation. The runner, the statistics and the design below are complete;
the numbers are not, and none are estimated in their place.

---

## Why this one

Every other quality signal in GigBid is a proxy. The Critic scores a draft, an
LLM judge applies a rubric, Recall@5 measures retrieval — all of them are
opinions about text, and all of them are produced by components that could in
principle be gamed by the thing they are judging.

GigSim is different. It holds **hidden client preferences** — a budget ceiling,
a dislike of generic openings, whether this client wants a question asked,
whether they require a named metric, a word cap — and decides reply, interview
or hire from them. Nothing in GigBid can see those preferences or optimise
against them directly.

That makes reply rate the only arbiter in the system that is not marking its
own homework, and the only one worth running an experiment against.

A/B testing a *prompt* rather than a model is also the realistic version of
this problem. A freelancer cannot retrain anything. What they can change is how
they write, and the question "does leading with the client's problem beat
leading with my credentials?" is the one they actually care about.

---

## Design

| Arm | Strategy |
| --- | --- |
| `evidence_led` | Opens with the client's problem in their words, names one concrete project with a measurable outcome, gives a concrete first step, ends with one specific question. This is what the shipped Writer prompt does. |
| `capability_led` | Opens by introducing the freelancer, summarises breadth of capabilities, explains fit, closes with availability. The register most freelancers actually write in. |

Four things keep it honest:

1. **Both arms are grounded identically.** The claim verifier runs on both, so
   unsupported claims are stripped from both. This isolates *framing* from
   *honesty* — otherwise a strategy could win by being allowed to exaggerate.
2. **Same jobs, same evidence.** Both arms draft for the same job from the same
   retrieved evidence. The only variable is the instruction.
3. **Drafting order alternates per job.** If the simulator has any ordering
   effect — a client who has already seen one proposal — it is shared between
   arms rather than handed to one.
4. **The world is reset first.** `POST /admin/reset` so proposals from earlier
   runs cannot contaminate the counts.

---

## Reporting

Per arm: submitted, replied, hired, reply rate, and a **Wilson 95% interval**
on that rate. The interval is there because at n≈40 per arm a difference of ten
points is well inside noise, and a bare percentage invites over-reading.

Across arms: a **two-proportion z-test**. The interpretation line states
plainly whether the difference is significant, and when it is not it says that
more jobs would be needed before preferring either strategy.

This is the part most likely to produce a negative result, and the runner is
written so a negative result reads as a finding rather than a failure. At the
sample sizes a laptop run affords, "not significant" is the expected and
correct answer, and reporting it beats quietly increasing n until something
crosses p<0.05.

---

## Honest caveat

**Reply rate here is GigSim's, not the world's.** The hidden preferences are a
deterministic model of what clients respond to — specific openings, named
metrics, a question, a word cap — and those weights were chosen by me. So this
experiment can tell you which strategy wins *against this model of a client*,
and it is a consistent arbiter for comparing system variants against each
other. It is not evidence about real Upwork clients.

The design would transfer unchanged to live mode, where reply rate would be
real. Live mode is never graded on outcomes, so that is left as the obvious
next step rather than claimed here.

**With no API key** both arms produce the same placeholder text, so the
comparison is meaningless. The results file records `provider: offline_stub`
and the runner prints a warning saying so.

---

## What it reuses

Nothing here is bolted on. The experiment runs through the same marketplace
port, the same idempotent writes, the same claim verifier and the same
retrieval stack as the product, which is why it is a test of GigBid rather than
a test of a script that resembles GigBid. The statistics come from
`evals/metrics.py`, unit-tested against hand-computed values in
`tests/unit/test_metrics.py`.
