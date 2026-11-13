"""Fit Matcher: map every requirement to portfolio evidence, or admit the gap.

This node owns retrieval for the whole bid graph - the Writer reuses what it
found rather than retrieving again, which keeps one retrieval design in play
per job and makes the RAG evaluation meaningful.

Its honesty is load-bearing. A requirement marked satisfied on thin evidence
becomes a claim in a proposal, and the only thing downstream that can catch it
is the claim verifier, working with less context than this node had.
"""

from __future__ import annotations

from typing import Any

from agent.handoffs import FitReport, JobSpec, RequirementMatch
from agent.llm import RunLedger
from agent.nodes.common import clamp, llm, node, system_message
from rag.retriever import get_variant
from rag.variants.base import RetrievalQuery


@node("fit_matcher")
async def match_fit(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    spec: JobSpec | None = state.get("job_spec")
    job = state.get("job") or {}
    persona_id = state.get("persona_id") or "p_ai_engineer"

    if spec is None:
        # Analyst failed. Retrieve on the raw post so the thread still produces
        # something a human can look at, and say the fit is unknown.
        query = RetrievalQuery(
            text=f"{job.get('title','')} {job.get('description','')}"[:2000],
            skills=job.get("required_skills", []),
            persona_id=persona_id,
            k=8,
        )
    else:
        query = RetrievalQuery(
            text=f"{spec.title}\n{spec.summary}",
            requirements=spec.requirements,
            skills=spec.required_skills,
            persona_id=persona_id,
            industry=(job.get("client") or {}).get("industry"),
            k=8,
            job_id=spec.job_id,
        )

    variant = get_variant()
    result = await variant.retrieve(query)
    evidence = result.to_evidence_items()

    if not evidence:
        return {
            "fit_report": FitReport(
                job_id=job.get("job_id", ""),
                score=0.0,
                reasoning="No portfolio evidence was retrieved for this job.",
                requirement_matches=[
                    RequirementMatch(requirement=r, satisfied=False, confidence=0.0,
                                     gap_note="no evidence retrieved")
                    for r in (spec.requirements if spec else [])
                ],
            ),
            "evidence": [],
            "retrieval_diagnostics": {
                "variant": result.variant,
                "latency_s": round(result.latency_s, 4),
                **result.diagnostics,
            },
        }

    bundle = "\n\n".join(
        f"[{item.evidence_id}] ({item.type}, {item.date}, outcome={item.outcome})\n"
        f"{item.title}\n{item.text[:800]}"
        for item in evidence[:10]
    )
    requirements = spec.requirements if spec else job.get("required_skills", [])
    user = (
        "REQUIREMENTS TO CHECK:\n"
        + "\n".join(f"{i+1}. {r}" for i, r in enumerate(requirements))
        + f"\n\nCLIENT'S LISTED SKILLS: {', '.join(spec.required_skills if spec else job.get('required_skills', []))}"
        + f"\n\nRETRIEVED PORTFOLIO EVIDENCE (the ONLY evidence that exists):\n{bundle}"
    )

    report, _ = await llm().structured(
        "fit_matcher", [system_message("fit_matcher"), {"role": "user", "content": user}],
        FitReport, ledger=ledger,
    )
    report.job_id = job.get("job_id", "")

    # --- guard the model's citations -------------------------------------
    valid_ids = {item.evidence_id for item in evidence}
    hallucinated = 0
    for match in report.requirement_matches:
        clean = [eid for eid in match.evidence_ids if eid in valid_ids]
        hallucinated += len(match.evidence_ids) - len(clean)
        match.evidence_ids = clean
        if match.satisfied and not clean:
            # Satisfied with no valid citation is not satisfied.
            match.satisfied = False
            match.confidence = min(match.confidence, 0.3)
            match.gap_note = (match.gap_note or "") + " (no valid evidence cited)"
    report.evidence_ids = [eid for eid in report.evidence_ids if eid in valid_ids] or [
        e.evidence_id for e in evidence[:5]
    ]
    if not report.strongest_evidence_id or report.strongest_evidence_id not in valid_ids:
        report.strongest_evidence_id = evidence[0].evidence_id

    # Recompute the score from coverage so it cannot drift from the matrix.
    if report.requirement_matches:
        coverage = sum(
            m.confidence if m.satisfied else 0.0 for m in report.requirement_matches
        ) / len(report.requirement_matches)
        report.score = clamp(0.65 * coverage + 0.35 * report.score)

    diagnostics = {
        "variant": result.variant,
        "latency_s": round(result.latency_s, 4),
        "evidence_count": len(evidence),
        "hallucinated_citations_dropped": hallucinated,
        "coverage": round(report.coverage, 3),
        **result.diagnostics,
    }
    # V8 can declare the evidence insufficient; that is a first-class signal.
    if result.diagnostics.get("insufficient_evidence"):
        report.score = min(report.score, 0.25)
        report.reasoning = (
            "Corrective retrieval judged the available evidence insufficient. " + report.reasoning
        )

    return {"fit_report": report, "evidence": evidence, "retrieval_diagnostics": diagnostics}
