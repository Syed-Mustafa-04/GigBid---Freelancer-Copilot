"""Compliance Guard: enforce the policy pack. A gate, not an advisor.

Rule *bodies* live in ``data/policy_pack/*.md`` so a policy change is a data
change, which is how the 2 November platform policy update is absorbed without
touching Python.

Deterministic detectors run first and are authoritative for anything with a
definite answer - an email address either is or is not present, and a model
should not get a vote. The model adds judgement for the rules that need
reading comprehension, such as whether a sentence promises a result.

This node can block. When it does, the draft does not reach the human as an
approvable item; it reaches them as a blocked item with the offending span.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.config import REPO_ROOT, get_config
from agent.handoffs import (
    ClaimVerification,
    ComplianceVerdict,
    ComplianceViolation,
    ProposalDraft,
    ReplyDraft,
)
from agent.llm import RunLedger
from agent.nodes.common import llm, node, system_message
from agent.safety import find_pii

GUARANTEE_RE = re.compile(
    r"\b(i\s+)?(guarantee|guaranteed|promise you|100%\s+(success|satisfaction)|"
    r"will\s+definitely\s+(rank|increase|double|triple)|"
    r"assured\s+results|no\s+risk\s+at\s+all)\b",
    re.IGNORECASE,
)
OFF_PLATFORM_RE = re.compile(
    r"\b(whatsapp|telegram|signal|skype|wechat|discord|gmail\.com|outlook\.com|"
    r"contact\s+me\s+(directly|at)|my\s+personal\s+(email|number)|"
    r"outside\s+(of\s+)?(the\s+)?platform|off[- ]platform)\b",
    re.IGNORECASE,
)
UNPAID_RE = re.compile(
    r"\b(happy\s+to\s+(do|work|build)\s+(this\s+)?(for\s+)?free|"
    r"free\s+(sample|trial|prototype|mockup)|no\s+charge\s+for\s+the\s+first|"
    r"unpaid\s+(trial|test))\b",
    re.IGNORECASE,
)
FABRICATION_RE = re.compile(
    r"\b(\d+\+?\s*years?\s+of\s+experience|extensive\s+experience|"
    r"proven\s+track\s+record|hundreds\s+of\s+(clients|projects)|"
    r"dozens\s+of\s+(clients|projects)|certified\s+expert)\b",
    re.IGNORECASE,
)

_POLICY_CACHE: dict[str, str] = {}
RULE_ID_RE = re.compile(r"^##\s+R-\d+\s+`([a-z_]+)`", re.MULTILINE)


def policy_rule_ids() -> set[str]:
    """Rule ids declared in the active policy pack.

    The guard may only BLOCK on a rule that exists in the pack. A model asked
    to find violations will occasionally return a plausible-looking rule id it
    invented, and an invented rule must not be able to stop a proposal the
    policy permits - that is an unaccountable veto. Unknown ids are kept, but
    downgraded to warnings and labelled.
    """
    return set(RULE_ID_RE.findall(load_policy()))


def load_policy() -> str:
    cfg = get_config()
    pack_dir = REPO_ROOT / str(cfg.get("policy.pack_path", "data/policy_pack"))
    active = str(cfg.get("policy.active_pack", "marketplace_policy.md"))
    key = f"{pack_dir}/{active}"
    if key in _POLICY_CACHE:
        return _POLICY_CACHE[key]
    path = Path(key)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    _POLICY_CACHE[key] = text
    return text


def _span(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    start = max(0, match.start() - 30)
    return text[start : match.end() + 30].replace("\n", " ").strip()


def deterministic_violations(
    text: str, *, verification: ClaimVerification | None, word_count: int, max_words: int
) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []

    pii = find_pii(text)
    leaked = {k: v for k, v in pii.items()
              if k in {"email", "phone", "messaging_handle", "crypto_wallet", "iban", "credit_card"}}
    if leaked:
        violations.append(
            ComplianceViolation(
                rule_id="pii_leak",
                rule_title="No personal contact or payment details",
                severity="block",
                evidence_span=", ".join(f"{k}: {v[0]}" for k, v in list(leaked.items())[:3]),
                remedy="Remove the contact details. All communication stays in the marketplace thread.",
            )
        )
    if OFF_PLATFORM_RE.search(text):
        violations.append(
            ComplianceViolation(
                rule_id="off_platform_contact",
                rule_title="No off-platform contact or payment",
                severity="block",
                evidence_span=_span(OFF_PLATFORM_RE, text),
                remedy="Remove the reference to communicating outside the platform.",
            )
        )
    if GUARANTEE_RE.search(text):
        violations.append(
            ComplianceViolation(
                rule_id="guaranteed_outcome",
                rule_title="No guaranteed results",
                severity="block",
                evidence_span=_span(GUARANTEE_RE, text),
                remedy="Replace the guarantee with what you will actually do and how it is measured.",
            )
        )
    if UNPAID_RE.search(text):
        violations.append(
            ComplianceViolation(
                rule_id="unpaid_production_work",
                rule_title="No unpaid production work",
                severity="block",
                evidence_span=_span(UNPAID_RE, text),
                remedy="Remove the offer of free work. A paid discovery phase is the alternative.",
            )
        )
    if FABRICATION_RE.search(text):
        violations.append(
            ComplianceViolation(
                rule_id="fabricated_experience",
                rule_title="No fabricated or unverifiable experience",
                severity="block",
                evidence_span=_span(FABRICATION_RE, text),
                remedy="State the specific project instead of an unverifiable experience total.",
            )
        )
    if verification is not None and verification.unsupported_after > 0:
        violations.append(
            ComplianceViolation(
                rule_id="fabricated_experience",
                rule_title="No fabricated or unverifiable experience",
                severity="block",
                evidence_span="; ".join(
                    c.text[:80] for c in verification.claims
                    if c.status.value == "unsupported" and not c.removed
                )[:300],
                remedy="Remove the unsupported claims flagged by the claim verifier.",
            )
        )
    if word_count > max_words:
        violations.append(
            ComplianceViolation(
                rule_id="overlong_proposal",
                rule_title="Proposal length limit",
                severity="warn",
                evidence_span=f"{word_count} words",
                remedy=f"Cut to {max_words} words.",
            )
        )
    return violations


@node("compliance", kind="proposal")
async def check_compliance(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    cfg = get_config()
    draft: ProposalDraft | ReplyDraft | None = state.get("draft") or state.get("reply")
    verification: ClaimVerification | None = state.get("claim_verification")

    if draft is None:
        return {"errors": ["compliance: nothing to check"]}

    if isinstance(draft, ProposalDraft):
        text = draft.cover_letter + "\n" + "\n".join(a.answer for a in draft.screening_answers)
        word_count = draft.word_count
    else:
        text = draft.body
        word_count = len(draft.body.split())

    max_words = int(cfg.get("policy.proposal_limits.max_words", 320))
    violations = deterministic_violations(
        text, verification=verification, word_count=word_count, max_words=max_words
    )
    found_ids = {v.rule_id for v in violations}

    # --- model pass for judgement rules ----------------------------------
    policy = load_policy()
    user = (
        f"POLICY PACK:\n{policy[:4000]}\n\n"
        f"TEXT TO CHECK:\n{text[:3000]}\n\n"
        f"Deterministic checks already found: {sorted(found_ids) or 'none'}. "
        "Report only ADDITIONAL violations those checks would have missed."
    )
    try:
        verdict, _ = await llm().structured(
            "compliance", [system_message("compliance"), {"role": "user", "content": user}],
            ComplianceVerdict, ledger=ledger,
        )
        known = policy_rule_ids()
        for violation in verdict.violations:
            if violation.rule_id in found_ids:
                continue
            if known and violation.rule_id not in known:
                violation.severity = "warn"
                violation.rule_title = (violation.rule_title or "Unrecognised rule")
                violation.remedy = (
                    f"{violation.remedy} (Downgraded: '{violation.rule_id}' is not a rule in "
                    f"the active policy pack, so it cannot block.)"
                ).strip()
            violations.append(violation)
            found_ids.add(violation.rule_id)
        explanation = verdict.explanation
    except Exception as exc:  # noqa: BLE001
        explanation = f"Model pass unavailable ({type(exc).__name__}); deterministic rules applied."

    block_on = set(cfg.get("policy.enforcement.block_on", []) or [])
    known = policy_rule_ids()
    for violation in violations:
        if violation.rule_id in block_on and (not known or violation.rule_id in known):
            violation.severity = "block"

    blocking = [v for v in violations if v.severity == "block"]
    final = ComplianceVerdict(
        job_id=state.get("job_id", ""),
        allowed=not blocking,
        violations=violations,
        checked_rules=sorted(block_on | set(cfg.get("policy.enforcement.warn_on", []) or [])),
        explanation=explanation
        or (f"{len(blocking)} blocking violation(s)." if blocking else "No blocking violations."),
    )

    update: dict[str, Any] = {"compliance": final, "status": "compliance"}
    if blocking:
        update["terminal_reason"] = (
            f"Blocked by the compliance guard: {', '.join(v.rule_id for v in blocking)}"
        )
    return update
