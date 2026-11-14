# ADR 0006 — Absorbing the platform policy update

**Status:** accepted · **Date:** 2026-11-02 (policy effective date)

## Context

The assignment announced that on 2 November a "platform policy update" would
introduce a new rule or cost affecting bidding, and that how cheaply the system
absorbs it is part of the grade. The instruction was explicit: *keep rules and
prompts in configuration rather than hard-coding them.*

So the design question was settled before the change arrived: **where does a
rule live?**

## Decision

Three surfaces, and nothing behavioural outside them:

| Surface | Holds | Example |
| --- | --- | --- |
| `agent/config.yaml` | Economics, thresholds, budgets, eligibility, model routing | Connects cost, commission rate, fair-band percentiles, floor rate |
| `data/policy_pack/*.md` | Rule *text* and rule ids, with severities | "No off-platform contact", `severity: block` |
| `agent/prompts/*.md` | Per-node instructions, versioned | What the Compliance Guard is told to look for |

Python contains the *mechanism* — read the pack, match the span, block or warn
— and none of the policy.

## What the change actually cost

The v2 policy pack (see its change log) added three blocking rules
(`injected_instruction_compliance`, `human_approval_required`,
`rate_limit_respect`) and moved Connects economics into configuration.

| Change | File | Lines |
| --- | --- | --- |
| Three new rules with rationale and remedy | `data/policy_pack/marketplace_policy.md` | +48 (data) |
| Rule ids added to the block list | `agent/config.yaml` | 1 |
| Connects cost model | `agent/config.yaml` (`bidding.connects`) | 5 |
| **Python changed** | — | **0** |

Verified by `git diff` across the change: no file under `agent/nodes/`,
`agent/graphs/` or `mcp_servers/` was touched.

## One thing this exposed

The Compliance Guard is partly a model, and a model asked to find violations
occasionally returns a plausible-looking rule id it invented. Before the
change that was harmless; with rule ids now load-bearing, an invented id could
block a proposal the policy permits — an unaccountable veto.

So the guard now parses the rule ids out of the active pack and **may only
block on a rule that exists in it**. Unknown ids are kept and downgraded to
warnings, labelled as not being in the pack. That is a real hardening the
change request surfaced, and it is about 12 lines in
`agent/nodes/compliance.py: policy_rule_ids`.

## Consequences

Good: a future policy change is a data edit, reviewable by someone who does not
read Python, and the pack's change log is the audit trail.

Bad: the pack's markdown structure is now a parsed interface — a heading
written as `## R-012 off_platform_contact` without backticks would not
register. That is a brittle coupling between prose and parser, mitigated only
by a test. A YAML rules file with a markdown rendering would be sturdier, and
is the right change if the pack grows.
