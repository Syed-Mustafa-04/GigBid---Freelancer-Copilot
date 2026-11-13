"""Job Analyst: untrusted job post -> structured JobSpec.

Holds no tools at all. Everything it needs is already in state, and giving a
node that reads attacker-controlled text the ability to call tools is exactly
the combination that makes prompt injection dangerous. Its allowlist in
``AGENT_TOOLS`` is empty, and that is a design decision rather than an
oversight.
"""

from __future__ import annotations

from typing import Any

from agent.handoffs import JobSpec, ScreeningQuestion
from agent.llm import RunLedger
from agent.nodes.common import node, system_message
from agent.safety import sanitise_job, wrap_untrusted


@node("analyst")
async def analyse_job(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    job = state.get("job") or {}
    safe_job, injection = sanitise_job(job)

    budget = job.get("budget") or {}
    context = (
        f"Job id: {job.get('job_id','')}\n"
        f"Budget: {budget.get('min')} - {budget.get('max')} {budget.get('currency','USD')} "
        f"({budget.get('type','fixed')})\n"
        f"Duration: {job.get('duration_days')} days\n"
        f"Listed skills: {', '.join(job.get('required_skills', []))}\n"
        f"Screening questions: {len(job.get('screening_questions', []))}\n"
    )
    untrusted = wrap_untrusted(
        f"TITLE: {safe_job.get('title','')}\n\n{safe_job.get('description','')}\n\n"
        + "\n".join(
            f"SCREENING QUESTION: {q.get('text','')}"
            for q in safe_job.get("screening_questions", [])
            if isinstance(q, dict)
        ),
        label="job post",
    )

    messages = [
        system_message("analyst"),
        {"role": "user", "content": f"{context}\n{untrusted}"},
    ]

    spec, _ = await llm_structured(messages, ledger)
    spec.job_id = job.get("job_id", "")

    # Screening questions are structural data; take them from the post rather
    # than trusting the model to copy them faithfully.
    spec.screening_questions = [
        ScreeningQuestion(
            question_id=q.get("question_id", f"sq{i}"),
            text=q.get("text", ""),
            required=bool(q.get("required", True)),
        )
        for i, q in enumerate(safe_job.get("screening_questions", []) or [])
        if isinstance(q, dict) and q.get("text")
    ]
    if not spec.required_skills:
        spec.required_skills = list(job.get("required_skills", []))
    if spec.budget_min is None:
        spec.budget_min = budget.get("min")
    if spec.budget_max is None:
        spec.budget_max = budget.get("max")
    if not spec.requirements:
        # A post with no extractable requirement is itself a finding.
        spec.ambiguity_notes.append("No atomic requirement could be extracted from this post.")

    if injection.detected:
        spec.ambiguity_notes.append(
            f"Post contains {len(injection.spans)} injection attempt(s): "
            f"{', '.join(sorted(set(injection.goals)))}. Instructions were ignored."
        )

    return {
        "job_spec": spec,
        "job_sanitised": safe_job,
        "status": "analysing",
    }


async def llm_structured(messages: list[dict[str, str]], ledger: RunLedger):
    from agent.nodes.common import llm

    return await llm().structured("analyst", messages, JobSpec, ledger=ledger)
