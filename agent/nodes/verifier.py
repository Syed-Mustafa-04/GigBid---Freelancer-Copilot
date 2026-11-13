"""Claim verification node (B3) - runs before the Critic, deliberately.

Ordering rationale, which the report states: the Critic judges persuasiveness,
and a fabricated sentence is frequently the most persuasive one in a draft.
Running the Critic first would have it defend exactly the sentences that must
be deleted, and then the deletion would look like a quality regression.

So: verify, strip, *then* critique what is left. The Critic only ever sees
true sentences, and its score means what it says.
"""

from __future__ import annotations

from typing import Any

from agent.config import get_config
from agent.handoffs import EvidenceItem, ProposalDraft
from agent.llm import RunLedger
from agent.nodes.common import node
from rag.verify import unsupported_rate, verify_draft


@node("claim_verifier", kind="proposal")
async def verify_claims(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    cfg = get_config()
    draft: ProposalDraft | None = state.get("draft")
    evidence: list[EvidenceItem] = state.get("evidence") or []

    if draft is None:
        return {"errors": ["claim_verifier: no draft to verify"]}

    use_llm = bool(cfg.get("rag.claim_verification_use_llm", False))
    cleaned, report = await verify_draft(draft, evidence, use_llm=use_llm)
    rates = unsupported_rate(report)

    min_ratio = float(cfg.get("thresholds.min_claim_support_ratio", 0.85))
    max_unsupported = int(cfg.get("thresholds.max_unsupported_claims", 0))

    errors: list[str] = []
    if report.unsupported_after > max_unsupported:
        # Should be unreachable: unsupported claims are removed, not kept.
        errors.append(
            f"claim_verifier: {report.unsupported_after} unsupported claims survived verification"
        )

    # A draft gutted by verification is a retrieval failure, not a writing one.
    if rates["claims_total"] >= 3 and report.support_ratio < min_ratio:
        errors.append(
            f"claim_verifier: support ratio {report.support_ratio:.2f} below {min_ratio:.2f} - "
            f"{rates['removed']} claims removed, evidence may be too weak for this job"
        )

    cleaned.status = "critiquing"
    return {
        "draft": cleaned,
        "claim_verification": report,
        "status": "verifying",
        "retrieval_diagnostics": {"claim_rates": rates, "verifier": "llm" if use_llm else "lexical"},
        **({"errors": errors} if errors else {}),
    }
