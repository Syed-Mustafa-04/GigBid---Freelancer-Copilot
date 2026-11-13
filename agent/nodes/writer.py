"""Proposal Writer: draft a proposal grounded in retrieved evidence.

Reuses the Fit Matcher's evidence rather than retrieving again, so one
retrieval design is in play per job and the RAG comparison stays honest.

Learned style rules from long-term memory are injected here - this is the only
place user edits change behaviour, and they arrive as explicit instructions
rather than as fine-tuning, so the user can read and delete them in Settings.
"""

from __future__ import annotations

from typing import Any

from agent.config import get_config
from agent.handoffs import (
    EvidenceItem,
    FitReport,
    JobSpec,
    PriceQuote,
    ProposalDraft,
    ScreeningAnswer,
)
from agent.llm import RunLedger
from agent.nodes.common import llm, node, system_message


def _evidence_bundle(evidence: list[EvidenceItem], limit: int = 8) -> str:
    return "\n\n".join(
        f"[{item.evidence_id}] ({item.type}, {item.date}"
        + (f", this proposal was {item.outcome}" if item.type == "proposal" and item.outcome != "unknown" else "")
        + f")\n{item.title}\n{item.text[:900]}"
        for item in evidence[:limit]
    )


def _style_block(state: dict[str, Any]) -> str:
    rules = state.get("style_rules") or []
    if not rules:
        return ""
    return (
        "LEARNED STYLE RULES (from this freelancer's own past edits - follow them):\n"
        + "\n".join(f"- {r}" for r in rules[:12])
    )


def _outcome_block(state: dict[str, Any]) -> str:
    rules = state.get("outcome_rules") or []
    if not rules:
        return ""
    return (
        "WHAT HAS ACTUALLY WORKED for this freelancer in this category:\n"
        + "\n".join(f"- {r}" for r in rules[:8])
    )


@node("writer", kind="proposal")
async def write_proposal(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    cfg = get_config()
    job = state.get("job") or {}
    spec: JobSpec | None = state.get("job_spec")
    fit: FitReport | None = state.get("fit_report")
    quote: PriceQuote | None = state.get("price_quote")
    evidence: list[EvidenceItem] = state.get("evidence") or []
    critique = state.get("critique")
    revision = int(state.get("revision_count", 0))

    max_words = int(cfg.get("policy.proposal_limits.max_words", 320))
    min_words = int(cfg.get("policy.proposal_limits.min_words", 90))

    satisfied = [m for m in (fit.requirement_matches if fit else []) if m.satisfied]
    gaps = [m for m in (fit.requirement_matches if fit else []) if not m.satisfied]

    sections = [
        f"JOB TITLE: {job.get('title','')}",
        f"CLIENT'S OWN WORDS:\n{(job.get('description') or '')[:1500]}",
        f"REQUIREMENTS WITH EVIDENCE ({len(satisfied)}):\n"
        + "\n".join(f"- {m.requirement} -> {', '.join(m.evidence_ids)}" for m in satisfied),
    ]
    if gaps:
        sections.append(
            "REQUIREMENTS WITH NO EVIDENCE (do NOT claim these; you may say how you would approach them):\n"
            + "\n".join(f"- {m.requirement}" for m in gaps)
        )
    if quote:
        sections.append(
            f"PRICE TO QUOTE: ${quote.amount_usd:,.0f} "
            f"({'per hour' if quote.is_hourly else 'fixed'}). Rationale: {quote.rationale}"
        )
    sections.append(f"EVIDENCE YOU MAY CITE (nothing else exists):\n{_evidence_bundle(evidence)}")
    if spec and spec.screening_questions:
        sections.append(
            "SCREENING QUESTIONS (answer every one):\n"
            + "\n".join(f"{i+1}. {q.text}" for i, q in enumerate(spec.screening_questions))
        )
    sections.append(f"WORD LIMIT: {min_words}-{max_words} words for the cover letter.")

    for block in (_style_block(state), _outcome_block(state)):
        if block:
            sections.append(block)

    if critique is not None and getattr(critique, "issues", None):
        sections.append(
            f"REVISION {revision + 1}. The previous draft was sent back. Fix exactly these:\n"
            + "\n".join(f"- [{i.severity}] {i.kind}: {i.detail}" for i in critique.issues)
            + f"\n\nInstructions: {critique.revision_instructions}"
        )

    draft, _ = await llm().structured(
        "writer",
        [system_message("writer"), {"role": "user", "content": "\n\n".join(sections)}],
        ProposalDraft,
        ledger=ledger,
    )

    # --- normalise what the model returned -------------------------------
    draft.job_id = job.get("job_id", "")
    draft.price_usd = quote.amount_usd if quote else 0.0
    draft.revision = revision
    draft.status = "verifying"

    valid_ids = {item.evidence_id for item in evidence}
    draft.evidence_ids = [eid for eid in draft.evidence_ids if eid in valid_ids]
    if not draft.evidence_ids and evidence:
        draft.evidence_ids = [e.evidence_id for e in evidence[:3]]

    # Ensure every screening question is answered, even if the model skipped one.
    if spec and spec.screening_questions:
        answered = {a.question.strip().lower() for a in draft.screening_answers}
        for question in spec.screening_questions:
            if question.text.strip().lower() not in answered:
                draft.screening_answers.append(
                    ScreeningAnswer(
                        question=question.text,
                        answer="",
                        evidence_ids=[],
                    )
                )
        # Drop answers to questions that were never asked.
        asked = {q.text.strip().lower() for q in spec.screening_questions}
        draft.screening_answers = [
            a for a in draft.screening_answers if a.question.strip().lower() in asked
        ]

    draft.recount()
    return {"draft": draft, "status": "drafting"}
