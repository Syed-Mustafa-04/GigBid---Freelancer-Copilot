"""Bid / skip / watch, with the expected value that justifies it.

Rules first, model second. The decision to spend Connects is arithmetic over
the fit score, the risk score, the client's hire rate and the Connects cost,
and arithmetic is auditable in a way a model's reasoning is not. The config
thresholds are the whole policy; changing bidding behaviour is a YAML edit.

The model is consulted only inside the ambiguous band, where the rules
genuinely cannot separate bid from watch.
"""

from __future__ import annotations

from typing import Any

from agent.config import get_config
from agent.handoffs import BidDecision, Decision, FitReport, RiskFlag, RiskReport
from agent.llm import RunLedger
from agent.nodes.common import node

BLOCKING_FLAGS = {
    RiskFlag.CREDENTIAL_REQUEST,
    RiskFlag.UNPAID_TRIAL,
    RiskFlag.FREE_WORK_REQUEST,
    RiskFlag.OFF_PLATFORM_CONTACT,
}


def expected_value(
    fit: float, client_hire_rate: float, budget_mid: float, competition: int
) -> float:
    """Crude but explicit: P(win) x value. Every term is visible in the UI.

    P(win) is fit scaled by how often this client hires at all and damped by
    the number of competing proposals. A client who never hires makes even a
    perfect fit worth approximately nothing, which is the judgement most
    freelancers get wrong by hand.
    """
    crowding = 1.0 / (1.0 + max(competition, 0) / 12.0)
    p_win = max(0.0, min(0.85, fit * (0.35 + 0.65 * client_hire_rate) * crowding))
    return p_win * budget_mid


@node("decision")
async def decide(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    cfg = get_config()
    job = state.get("job") or {}
    fit: FitReport | None = state.get("fit_report")
    risk: RiskReport | None = state.get("risk_report")
    client = state.get("client") or {}

    min_fit = float(cfg.get("thresholds.minimum_fit_score", 0.60))
    max_risk = float(cfg.get("thresholds.maximum_risk_score", 0.70))
    watch_low = float(cfg.get("thresholds.watch_band.fit_low", 0.50))
    min_ev_per_connect = float(cfg.get("bidding.connects.min_expected_value_per_connect_usd", 6.0))
    min_rating = float(cfg.get("bidding.eligibility.min_client_rating", 3.0))
    min_hire_rate = float(cfg.get("bidding.eligibility.skip_if_hires_rate_below", 0.05))
    require_verified = bool(cfg.get("bidding.eligibility.require_payment_verified", False))

    fit_score = fit.score if fit else 0.0
    risk_score = risk.score if risk else 0.5
    flags = set(risk.flags) if risk else set()

    budget = job.get("budget") or {}
    budget_mid = (float(budget.get("min") or 0) + float(budget.get("max") or 0)) / 2 or 0.0
    connects = int(job.get("connects_cost") or cfg.get("bidding.connects.base_cost_per_proposal", 8))
    hire_rate = float(client.get("hire_rate") or 0.0)
    competition = int(job.get("proposals_received") or 0)

    ev = expected_value(fit_score, hire_rate, budget_mid, competition)
    ev_per_connect = ev / max(connects, 1)

    reasons: list[str] = []
    blocking = sorted(flags & BLOCKING_FLAGS, key=lambda f: f.value)
    decision = Decision.BID
    confidence = 0.7

    # --- hard stops -------------------------------------------------------
    if risk and risk.recommended_action == "block":
        decision, confidence = Decision.SKIP, 0.95
        reasons.append(f"Risk assessor recommends blocking (score {risk_score:.2f}).")
    elif blocking:
        decision, confidence = Decision.SKIP, 0.95
        reasons.append(f"Policy-blocking signals present: {', '.join(f.value for f in blocking)}.")
    elif risk_score > max_risk:
        decision, confidence = Decision.SKIP, 0.85
        reasons.append(f"Risk {risk_score:.2f} exceeds the configured ceiling {max_risk:.2f}.")
    elif fit_score < watch_low:
        decision, confidence = Decision.SKIP, 0.8
        reasons.append(f"Fit {fit_score:.2f} is below the watch floor {watch_low:.2f}.")
    elif client and float(client.get("avg_rating") or 5.0) < min_rating:
        decision, confidence = Decision.SKIP, 0.75
        reasons.append(f"Client rating {client.get('avg_rating')} is below {min_rating}.")
    elif client and int(client.get("jobs_posted") or 0) >= 8 and hire_rate < min_hire_rate:
        decision, confidence = Decision.SKIP, 0.75
        reasons.append(
            f"Client has posted {client.get('jobs_posted')} jobs with a {hire_rate:.0%} hire rate."
        )
    elif require_verified and client and not client.get("payment_verified"):
        decision, confidence = Decision.SKIP, 0.7
        reasons.append("Client payment is not verified and config requires it.")
    # --- the band ---------------------------------------------------------
    elif fit_score < min_fit:
        decision, confidence = Decision.WATCH, 0.6
        reasons.append(
            f"Fit {fit_score:.2f} sits between the watch floor and the bid threshold "
            f"{min_fit:.2f}; worth monitoring, not worth Connects yet."
        )
    elif ev_per_connect < min_ev_per_connect:
        decision, confidence = Decision.WATCH, 0.65
        reasons.append(
            f"Expected value per Connect is ${ev_per_connect:.2f}, below the "
            f"${min_ev_per_connect:.2f} floor ({competition} competing proposals)."
        )
    else:
        reasons.append(
            f"Fit {fit_score:.2f} and risk {risk_score:.2f} are within thresholds; "
            f"expected value ${ev:.0f} over {connects} Connects (${ev_per_connect:.2f} each)."
        )
        if fit:
            satisfied = sum(1 for m in fit.requirement_matches if m.satisfied)
            reasons.append(f"{satisfied}/{len(fit.requirement_matches)} requirements have direct evidence.")

    if risk and risk.injection_detected:
        reasons.append(
            "Note: this post contained an instruction aimed at an automated assistant. "
            "It was ignored and the job judged on its own merits."
        )

    return {
        "decision": BidDecision(
            job_id=job.get("job_id", ""),
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            blocking_flags=blocking,
            expected_value_usd=round(ev, 2),
            connects_cost=connects,
            expected_value_per_connect=round(ev_per_connect, 2),
            decided_by="rules",
        ),
        "status": "deciding",
    }
