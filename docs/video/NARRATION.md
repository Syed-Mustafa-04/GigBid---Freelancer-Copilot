# Demo narration script

Roughly 430 words, about three minutes at an unhurried pace. Timings are cues,
not constraints — the video holds on each step long enough to land the line.

Every claim below is one the repository can back up. Nothing here asserts a
result that is not in `evals/results/`, which matters because the whole
argument of this project is that unverified claims are the failure mode.

---

### 0:00 — Sign in

> This is GigBid. It finds freelance work, decides whether a job is worth
> bidding on, writes the proposal, and manages the client conversation
> afterwards. Ten agents on a LangGraph state machine.
>
> The one rule the whole design is built around: nothing leaves the system
> without a human approving it.

### 0:20 — Job inbox

> Every job is scored before a single Connect is spent. Connects are the real
> currency here — they're finite, and spending them is irreversible, so the
> decision to bid is itself a decision worth evaluating.
>
> Fit, risk, and cost are on every card.

### 0:45 — The scam job

> This one asks for an entire SaaS platform for fifty dollars. It's flagged and
> skipped, and the flags that caused it are shown rather than hidden behind a
> score.
>
> A system that bids on everything burns a freelancer's Connects for them.

### 1:05 — Analyse, streaming

> Three agents analyse the job in parallel — requirements, portfolio fit, and
> client risk — and each reports as it finishes. That's server-sent events, not
> a spinner: you can see which agent is working and which one stalled.

### 1:30 — The approval gate

> Execution stops here. The graph has done everything it can do on its own and
> it will not proceed.
>
> Before approving, you see the exact Connects cost and the idempotency key
> that makes submission exactly-once. Approve the same proposal twice and it
> spends once — in the committed trace, Connects go from 120 to 112, and a
> replayed approval spends nothing further.

### 1:55 — Grounding

> Every claim in the proposal is traceable to a specific piece of portfolio
> evidence. Claims the verifier cannot support are removed before a human ever
> reads the draft.

### 2:15 — The finding worth stating

> The most useful result was a negative one. I wrote pattern-based injection
> detection and it caught eight out of eight planted attacks. Against eight
> held-out attacks it had never seen, it caught zero.
>
> To check that wasn't just bad regexes, I ran Meta's Prompt Guard 2, a model
> trained for this. It also scored zero.
>
> So detection is the wrong layer. What actually works is that no analysis
> agent holds a write tool, and the one that does refuses without a human
> approval token. Six out of six attempts to reach a write tool were blocked,
> asserted by test.

### 2:45 — Close

> Retrieval was chosen by measurement, not preference — ten variants on ninety
> labelled jobs. The ablation then contradicted the shipped choice, and that's
> written down in the architecture decision record rather than quietly fixed.
>
> Everything measured is in the repo. Everything not measured says so.

---

## What this recording is, exactly

The capture was made against a **local stack running the deterministic offline
provider**, not against the live deployment, and that is a deliberate choice
rather than a convenience.

The approval gate is the one thing the brief actually turns on, and a job only
reaches it after the analysis fan-out and a draft. On the Groq free tier that
sequence does not complete: three analyses run in parallel against a 12,000
token-per-minute ceiling, and the run stalls after `run_started`. I confirmed
this on both the Render deployment and locally with the live key, so it is the
quota, not the host.

The offline provider runs the identical graph and reaches the gate reliably,
which is what makes the structural claims visible: the stop, the dry-run cost,
the idempotency key, the spend, the pipeline move. What it does **not** produce
is real prose or real scores -- those are placeholders.

The app says so itself. Running without a key raises a banner reading "GigBid
is running on the deterministic offline provider: the graph executes but the
text and scores it produces are placeholders, not model output." That banner is
visible in the recording. **Leave it in, and say so in the voiceover.** A
viewer who spots it after being told the text was model-written has caught you
overclaiming; a viewer who was told up front has seen you handle a constraint
honestly, which is the thing this project is actually about.

Suggested line, around the approval gate:

> The model here is the deterministic offline provider -- the free tier's rate
> limit can't carry the parallel analysis, so the proposal text is placeholder.
> What's real is the control flow: the stop, the cost, the key, and the spend.

## Notes before you record

- **Warm both URLs first.** Free instances sleep after fifteen minutes; a cold
  open takes about a minute and looks like a hang.
- **Don't claim the live deployment reproduces the evaluation numbers.** It runs
  the hash embedding backend because a 512 MB instance will not hold torch. The
  measured figures used bge-small. This is stated in the README and in
  `docs/DEPLOY.md`.
- **Don't claim the five pending evaluations.** `decision_quality`,
  `multi_agent_comparison`, `proposal_experiment`, `judge_calibration` and
  `gigsim_outcomes` were never produced — the free-tier quota ran out. The
  report marks them pending rather than estimating them, and that is a
  defensible position to state out loud if asked.
