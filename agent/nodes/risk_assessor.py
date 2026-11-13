"""Risk Assessor: scam detection, client quality, injection reporting.

Deliberately combines two signals the model is bad at on its own:

* **Deterministic detection** for things with a definite answer - injection
  patterns, off-platform contact, impossible budgets. A regex does not get
  talked out of its verdict by a persuasive job post, which is the specific
  failure mode of asking a model to judge text that is attacking it.
* **Model judgement** for everything requiring reading comprehension - is this
  scope vague, is this urgency manufactured, does this read like fraud.

The two are combined by taking the maximum, because a miss costs the user
money and a false positive costs them one job they can override.
"""

from __future__ import annotations

from typing import Any

from agent.handoffs import RiskFlag, RiskReport
from agent.llm import RunLedger
from agent.nodes.common import call_tool, clamp, llm, node, system_message
from agent.safety import detect_injection, wrap_untrusted

# Deterministic markers, checked before the model sees anything.
HARD_MARKERS: list[tuple[RiskFlag, tuple[str, ...], float]] = [
    (RiskFlag.OFF_PLATFORM_CONTACT,
     ("telegram", "whatsapp", "signal app", "off platform", "off-platform",
      "outside the platform", "wechat", "skype me", "personal email"), 0.75),
    (RiskFlag.CREDENTIAL_REQUEST,
     ("your login", "your password", "account credentials", "copy of your id",
      "verify your account", "upwork login"), 0.9),
    (RiskFlag.UNPAID_TRIAL,
     ("unpaid trial", "unpaid test", "for free as a test", "no payment for submissions",
      "free work sample", "complete a sample"), 0.8),
    (RiskFlag.FREE_WORK_REQUEST,
     ("build it for free", "work for free", "free of charge as", "best submission wins"), 0.8),
    (RiskFlag.SUSPICIOUS_URGENCY,
     ("start immediately", "today only", "urgent:", "asap or we move on", "right now"), 0.3),
]


def _strip_injection(text: str, spans: list[str]) -> str:
    """Remove injected spans before judging the CLIENT.

    A third party can poison a legitimate post. If the deterministic markers
    read the injected text, an attacker can make any job look like a scam -
    and, worse, can make GigBid skip a good job by writing "contact me on
    WhatsApp" into someone else's listing. Client-risk signals are therefore
    computed on the post with the injected spans removed; the injection is
    reported separately as an attack on the agent, not as evidence about the
    client.
    """
    cleaned = text
    for span in spans:
        cleaned = cleaned.replace(span, " ")
    return cleaned


def _deterministic(job: dict[str, Any], injection_spans: list[str] | None = None) -> tuple[set[RiskFlag], float, list[str]]:
    blob = _strip_injection(
        f"{job.get('title','')} {job.get('description','')}", injection_spans or []
    ).lower()
    flags: set[RiskFlag] = set()
    score = 0.0
    hits: list[str] = []
    for flag, markers, weight in HARD_MARKERS:
        for marker in markers:
            if marker in blob:
                flags.add(flag)
                score = max(score, weight)
                hits.append(marker)
                break

    budget = job.get("budget") or {}
    budget_max = float(budget.get("max") or 0)
    description_length = len(job.get("description", ""))
    if budget_max == 0 and description_length > 120:
        flags.add(RiskFlag.BUDGET_MISMATCH)
        score = max(score, 0.6)
        hits.append("zero budget for a substantive scope")
    elif 0 < budget_max < 60 and description_length > 400:
        flags.add(RiskFlag.BUDGET_MISMATCH)
        score = max(score, 0.65)
        hits.append(f"budget {budget_max} for a long scope")

    if description_length < 90:
        flags.add(RiskFlag.VAGUE_SCOPE)
        score = max(score, 0.25)
        hits.append("very short description")
    return flags, score, hits


def _client_quality(client: dict[str, Any]) -> tuple[set[RiskFlag], float, float]:
    """Returns (flags, risk_contribution, quality_score)."""
    if not client:
        return set(), 0.1, 0.5
    flags: set[RiskFlag] = set()
    rating = float(client.get("avg_rating") or 0)
    hire_rate = float(client.get("hire_rate") or 0)
    posted = int(client.get("jobs_posted") or 0)
    verified = bool(client.get("payment_verified"))

    risk = 0.0
    if not verified:
        flags.add(RiskFlag.UNVERIFIED_PAYMENT)
        risk = max(risk, 0.3)
    if rating and rating < 3.0:
        flags.add(RiskFlag.LOW_CLIENT_RATING)
        risk = max(risk, 0.45)
    if posted >= 8 and hire_rate < 0.05:
        flags.add(RiskFlag.NO_HIRE_HISTORY)
        risk = max(risk, 0.4)

    quality = clamp(
        0.45 * (rating / 5.0 if rating else 0.4)
        + 0.35 * hire_rate
        + 0.20 * (1.0 if verified else 0.0)
    )
    return flags, risk, quality


@node("risk_assessor")
async def assess_risk(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    job = state.get("job") or {}
    job_id = job.get("job_id", "")

    # Client history is the one tool this node is allowed.
    client: dict[str, Any] = state.get("client") or {}
    degraded: list[str] = []
    if not client and job.get("client_id"):
        try:
            client = await call_tool(
                "risk_assessor", "marketplace", "get_client_history",
                {"client_id": job["client_id"]},
            )
        except Exception as exc:  # noqa: BLE001
            degraded.append("marketplace")
            client = {}
            state.setdefault("errors", []).append(f"risk_assessor: client history unavailable: {exc}")

    raw_text = f"{job.get('title','')}\n{job.get('description','')}\n" + "\n".join(
        q.get("text", "") for q in job.get("screening_questions", []) or [] if isinstance(q, dict)
    )
    injection = detect_injection(raw_text)
    det_flags, det_score, det_hits = _deterministic(job, injection.spans)
    client_flags, client_risk, quality = _client_quality(client)

    # --- model judgement -------------------------------------------------
    summary = (
        f"Client record: rating={client.get('avg_rating','?')}, "
        f"hire_rate={client.get('hire_rate','?')}, jobs_posted={client.get('jobs_posted','?')}, "
        f"payment_verified={client.get('payment_verified','?')}, "
        f"total_spend_usd={client.get('total_spend_usd','?')}\n"
        f"Budget: {(job.get('budget') or {}).get('min')} - {(job.get('budget') or {}).get('max')}\n"
        f"Deterministic markers already found: {det_hits or 'none'}\n"
        f"Injection scan: {'DETECTED - ' + ', '.join(sorted(set(injection.goals))) if injection.detected else 'clean'}"
    )
    messages = [
        system_message("risk_assessor"),
        {"role": "user", "content": f"{summary}\n\n{wrap_untrusted(raw_text, label='job post')}"},
    ]

    try:
        report, _ = await llm().structured("risk_assessor", messages, RiskReport, ledger=ledger)
    except Exception as exc:  # noqa: BLE001 - fall back to the deterministic verdict
        report = RiskReport(
            score=max(det_score, client_risk),
            explanation=f"Model unavailable ({type(exc).__name__}); deterministic markers only.",
        )

    # --- combine ---------------------------------------------------------
    report.job_id = job_id
    all_flags = set(report.flags) | det_flags | client_flags
    if injection.detected:
        all_flags.add(RiskFlag.PROMPT_INJECTION)
    report.flags = sorted(all_flags, key=lambda f: f.value)
    report.score = clamp(max(report.score, det_score, client_risk))
    report.client_quality_score = quality
    report.injection_detected = injection.detected
    report.injection_spans = injection.spans[:5]
    if not report.scam_probability:
        report.scam_probability = report.score

    scam_block = float(
        state.get("_scam_block_score")
        or __import__("agent.config", fromlist=["get_config"]).get_config().get("thresholds.scam_block_score", 0.8)
    )
    if report.score >= scam_block:
        report.recommended_action = "block"
    elif report.score >= 0.4 or injection.detected:
        report.recommended_action = "proceed_with_caution"
    else:
        report.recommended_action = "proceed"

    update: dict[str, Any] = {"risk_report": report, "client": client}
    if degraded:
        update["degraded_services"] = degraded
    return update
