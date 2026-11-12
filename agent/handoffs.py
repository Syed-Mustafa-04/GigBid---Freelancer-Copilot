"""Typed hand-offs between agents (C3).

Every object a later step *decides on* is one of these models. Free text is
allowed only inside a field that is explicitly prose (a cover letter, a reason
string). Nothing downstream parses another agent's prose.

These are also the structured-output schemas handed to the LLM, so the field
descriptions below are load-bearing: they are what the model is told to fill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class Decision(str, Enum):
    BID = "bid"
    SKIP = "skip"
    WATCH = "watch"


class RiskFlag(str, Enum):
    OFF_PLATFORM_CONTACT = "off_platform_contact"
    FREE_WORK_REQUEST = "free_work_request"
    UNPAID_TRIAL = "unpaid_trial"
    PROMPT_INJECTION = "prompt_injection"
    VAGUE_SCOPE = "vague_scope"
    BUDGET_MISMATCH = "budget_mismatch"
    UNVERIFIED_PAYMENT = "unverified_payment"
    LOW_CLIENT_RATING = "low_client_rating"
    NO_HIRE_HISTORY = "no_hire_history"
    CREDENTIAL_REQUEST = "credential_request"
    SUSPICIOUS_URGENCY = "suspicious_urgency"
    REPOSTED_DUPLICATE = "reposted_duplicate"


class ClaimStatus(str, Enum):
    SUPPORTED = "supported"
    PARTLY_SUPPORTED = "partly_supported"
    UNSUPPORTED = "unsupported"


class MessageIntent(str, Enum):
    QUESTION = "question"
    NEGOTIATION = "negotiation"
    INTERVIEW_REQUEST = "interview_request"
    SCAM_SIGNAL = "scam_signal"
    SCOPE_CHANGE = "scope_change"
    REJECTION = "rejection"
    OFFER = "offer"
    SMALL_TALK = "small_talk"


class ApprovalAction(str, Enum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
class EvidenceItem(Base):
    """One retrieved portfolio fragment. ``evidence_id`` is what a claim cites."""

    evidence_id: str
    freelancer_id: str = ""
    type: Literal["case_study", "repo", "code", "readme", "review", "proposal", "cv"] = "case_study"
    title: str = ""
    text: str = ""
    skills: list[str] = Field(default_factory=list)
    project_id: str | None = None
    client_industry: str | None = None
    date: str | None = None
    outcome: Literal["won", "replied", "ignored", "lost", "delivered", "unknown"] = "unknown"
    source: str = ""
    score: float = 0.0
    retrieved_by: str = ""

    def citation(self) -> str:
        return f"[{self.evidence_id}]"


class RequirementMatch(Base):
    """One row of the requirement-to-evidence matrix shown on the job page."""

    requirement: str
    satisfied: bool
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    evidence_ids: list[str] = Field(default_factory=list)
    gap_note: str = ""


# ---------------------------------------------------------------------------
# Job Analyst
# ---------------------------------------------------------------------------
class ScreeningQuestion(Base):
    question_id: str = ""
    text: str
    required: bool = True


class JobSpec(Base):
    """Structured reading of an untrusted job post."""

    job_id: str
    title: str
    summary: str = Field(default="", description="Two sentences, neutral, no marketing language")
    deliverables: list[str] = Field(
        default_factory=list, description="Concrete artefacts the client will receive"
    )
    required_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(
        default_factory=list,
        description="Atomic, independently checkable requirements used for retrieval",
    )
    hidden_needs: list[str] = Field(
        default_factory=list,
        description="Needs implied but not stated, e.g. handover docs, stakeholder demos",
    )
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str = "USD"
    is_hourly: bool = False
    estimated_hours: float | None = None
    timeline_days: int | None = None
    screening_questions: list[ScreeningQuestion] = Field(default_factory=list)
    complexity: Literal["low", "medium", "high"] = "medium"
    ambiguity_notes: list[str] = Field(default_factory=list)

    @field_validator("requirements")
    @classmethod
    def _cap_requirements(cls, value: list[str]) -> list[str]:
        return value[:12]


# ---------------------------------------------------------------------------
# Risk Assessor
# ---------------------------------------------------------------------------
class RiskReport(Base):
    job_id: str = ""
    score: float = Field(ge=0.0, le=1.0, description="0 safe, 1 certain scam")
    flags: list[RiskFlag] = Field(default_factory=list)
    scam_probability: float = Field(ge=0.0, le=1.0, default=0.0)
    injection_detected: bool = False
    injection_spans: list[str] = Field(
        default_factory=list, description="Verbatim spans that tried to issue instructions"
    )
    client_quality_score: float = Field(ge=0.0, le=1.0, default=0.5)
    explanation: str = ""
    recommended_action: Literal["proceed", "proceed_with_caution", "block"] = "proceed"


# ---------------------------------------------------------------------------
# Fit Matcher
# ---------------------------------------------------------------------------
class FitReport(Base):
    job_id: str = ""
    score: float = Field(ge=0.0, le=1.0)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    requirement_matches: list[RequirementMatch] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    strongest_evidence_id: str | None = None
    reasoning: str = ""

    @property
    def coverage(self) -> float:
        if not self.requirement_matches:
            return 0.0
        return sum(1 for m in self.requirement_matches if m.satisfied) / len(
            self.requirement_matches
        )


# ---------------------------------------------------------------------------
# Decision and price
# ---------------------------------------------------------------------------
class BidDecision(Base):
    job_id: str = ""
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reasons: list[str] = Field(default_factory=list)
    blocking_flags: list[RiskFlag] = Field(default_factory=list)
    expected_value_usd: float = 0.0
    connects_cost: int = 0
    expected_value_per_connect: float = 0.0
    decided_by: Literal["rules", "model", "human"] = "rules"


class PriceQuote(Base):
    job_id: str = ""
    amount_usd: float = Field(ge=0.0)
    is_hourly: bool = False
    fair_band_low: float = 0.0
    fair_band_high: float = 0.0
    market_median: float = 0.0
    effective_hourly_usd: float = 0.0
    inside_fair_band: bool = True
    rationale: str = ""
    comparable_job_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Proposal, claims, critique, compliance
# ---------------------------------------------------------------------------
class Claim(Base):
    """One checkable assertion extracted from a draft (B3)."""

    claim_id: str
    text: str
    location: Literal["cover_letter", "screening_answer"] = "cover_letter"
    answer_index: int | None = None
    status: ClaimStatus = ClaimStatus.UNSUPPORTED
    evidence_ids: list[str] = Field(default_factory=list)
    support_score: float = Field(ge=0.0, le=1.0, default=0.0)
    verifier_note: str = ""
    rewritten_as: str | None = None
    removed: bool = False


class ScreeningAnswer(Base):
    question: str
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProposalDraft(Base):
    job_id: str = ""
    cover_letter: str = ""
    screening_answers: list[ScreeningAnswer] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    price_usd: float = 0.0
    revision: int = 0
    word_count: int = 0
    status: Literal[
        "drafting", "verifying", "critiquing", "pending_approval", "approved", "rejected", "submitted"
    ] = "drafting"
    created_at: datetime = Field(default_factory=_utcnow)

    def recount(self) -> ProposalDraft:
        words = len(self.cover_letter.split())
        words += sum(len(a.answer.split()) for a in self.screening_answers)
        self.word_count = words
        return self


class ClaimVerification(Base):
    """Result of B3, reported before and after rewriting."""

    job_id: str = ""
    claims: list[Claim] = Field(default_factory=list)
    unsupported_before: int = 0
    unsupported_after: int = 0
    support_ratio: float = Field(ge=0.0, le=1.0, default=0.0)
    passed: bool = False

    @classmethod
    def from_claims(cls, job_id: str, claims: list[Claim], unsupported_before: int) -> ClaimVerification:
        live = [c for c in claims if not c.removed]
        unsupported_after = sum(1 for c in live if c.status == ClaimStatus.UNSUPPORTED)
        supported = sum(1 for c in live if c.status == ClaimStatus.SUPPORTED)
        ratio = supported / len(live) if live else 1.0
        return cls(
            job_id=job_id,
            claims=claims,
            unsupported_before=unsupported_before,
            unsupported_after=unsupported_after,
            support_ratio=ratio,
            passed=unsupported_after == 0,
        )


class CritiqueIssue(Base):
    kind: Literal[
        "ungrounded_claim",
        "generic_opening",
        "too_long",
        "too_short",
        "missing_requirement",
        "weak_personalisation",
        "unclear_next_step",
        "tone",
    ]
    detail: str
    span: str = ""
    severity: Literal["minor", "major", "blocking"] = "minor"


class Critique(Base):
    job_id: str = ""
    approved: bool = False
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    issues: list[CritiqueIssue] = Field(default_factory=list)
    revision_instructions: str = ""
    revision_number: int = 0

    @property
    def has_blocking(self) -> bool:
        return any(i.severity == "blocking" for i in self.issues)


class ComplianceViolation(Base):
    rule_id: str
    rule_title: str = ""
    severity: Literal["warn", "block"] = "block"
    evidence_span: str = ""
    remedy: str = ""


class ComplianceVerdict(Base):
    job_id: str = ""
    allowed: bool = True
    violations: list[ComplianceViolation] = Field(default_factory=list)
    redactions: list[str] = Field(
        default_factory=list, description="Spans removed before the draft may be shown"
    )
    checked_rules: list[str] = Field(default_factory=list)
    explanation: str = ""

    @property
    def blocking(self) -> list[ComplianceViolation]:
        return [v for v in self.violations if v.severity == "block"]


# ---------------------------------------------------------------------------
# Conversation graph
# ---------------------------------------------------------------------------
class InterviewSlot(Base):
    start_iso: str
    end_iso: str
    timezone_name: str = "UTC"
    label: str = ""


class ClientMessage(Base):
    message_id: str
    thread_id: str
    job_id: str = ""
    sender: Literal["client", "freelancer", "system"] = "client"
    body: str = ""
    created_at: str = ""


class IntentReport(Base):
    thread_id: str = ""
    intent: MessageIntent
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    urgency: Literal["low", "normal", "high"] = "normal"
    injection_detected: bool = False
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    summary: str = ""


class ReplyDraft(Base):
    thread_id: str = ""
    body: str = ""
    proposed_slots: list[InterviewSlot] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["drafting", "pending_approval", "approved", "rejected", "sent"] = "drafting"
    rationale: str = ""


# ---------------------------------------------------------------------------
# Client brief (signature extension X4)
# ---------------------------------------------------------------------------
class ClientBrief(Base):
    client_id: str
    display_name: str = ""
    total_spend_usd: float = 0.0
    hire_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    avg_rating: float = 0.0
    jobs_posted: int = 0
    payment_verified: bool = False
    industries: list[str] = Field(default_factory=list)
    hiring_pattern: str = ""
    talking_points: list[str] = Field(default_factory=list)
    caution_notes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Human approval (C5 / R4)
# ---------------------------------------------------------------------------
class DryRunPreview(Base):
    """Exactly what would be sent and what it costs, shown on the approval screen."""

    tool_name: str
    server: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    connects_cost: int = 0
    estimated_charge_usd: float = 0.0
    idempotency_key: str = ""
    would_call: str = ""


class ApprovalRequest(Base):
    """The interrupt payload. Carries everything the human needs to decide."""

    kind: Literal["proposal", "message", "connects_spend"]
    thread_id: str
    job_id: str = ""
    draft: ProposalDraft | None = None
    reply: ReplyDraft | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    risk_report: RiskReport | None = None
    price_quote: PriceQuote | None = None
    critique: Critique | None = None
    compliance: ComplianceVerdict | None = None
    dry_run: DryRunPreview | None = None
    budget_snapshot: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=_utcnow)


class ApprovalResponse(Base):
    action: ApprovalAction
    edited_cover_letter: str | None = None
    edited_screening_answers: list[ScreeningAnswer] | None = None
    edited_price_usd: float | None = None
    edited_reply_body: str | None = None
    reason: str = ""
    responded_by: str = ""
    responded_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Outcomes and memory
# ---------------------------------------------------------------------------
class Outcome(Base):
    job_id: str
    proposal_id: str = ""
    category: str = ""
    state: Literal["drafted", "submitted", "viewed", "replied", "interview", "hired", "lost", "ignored"]
    observed_at: datetime = Field(default_factory=_utcnow)
    rate_achieved_usd: float | None = None


class MemoryRule(Base):
    """A promoted long-term memory (R7). Never written from raw tool output."""

    rule_id: str
    store: Literal["style", "outcome"]
    text: str
    origin: Literal["user_edit", "observed_outcome"]
    occurrences: int = 1
    categories: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)
    evidence_refs: list[str] = Field(default_factory=list)


__all__ = [
    "ApprovalAction",
    "ApprovalRequest",
    "ApprovalResponse",
    "BidDecision",
    "Claim",
    "ClaimStatus",
    "ClaimVerification",
    "ClientBrief",
    "ClientMessage",
    "ComplianceVerdict",
    "ComplianceViolation",
    "Critique",
    "CritiqueIssue",
    "Decision",
    "DryRunPreview",
    "EvidenceItem",
    "FitReport",
    "IntentReport",
    "InterviewSlot",
    "JobSpec",
    "MemoryRule",
    "MessageIntent",
    "Outcome",
    "PriceQuote",
    "ProposalDraft",
    "ReplyDraft",
    "RequirementMatch",
    "RiskFlag",
    "RiskReport",
    "ScreeningAnswer",
    "ScreeningQuestion",
]
