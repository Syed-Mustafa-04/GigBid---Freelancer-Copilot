"""Critic: quality review with a bounded loop (C4).

Sends a draft back at most ``budgets.max_revisions`` times. On the final pass
it approves anything merely imperfect and records the remaining issues for the
human, because a thread blocked forever is worse for the user than a proposal
they can edit in the studio.

Deterministic checks run alongside the model: an opening matched against the
known-bad list and a word count are facts, and a model asked whether an
opening is generic will often say no about a sentence that begins "I am an
experienced professional".
"""

from __future__ import annotations

import re
from typing import Any

from agent.config import get_config
from agent.handoffs import Critique, CritiqueIssue, JobSpec, ProposalDraft
from agent.llm import RunLedger
from agent.nodes.common import llm, node, system_message

GENERIC_OPENINGS = [
    "i am an experienced", "i have a passion", "dear hiring manager",
    "i am writing to express", "i believe i am the perfect", "greetings of the day",
    "i am the best candidate", "hope this message finds you well", "i am highly skilled",
    "with over", "i am excited to apply", "thank you for considering",
]
MARKETING_WORDS = re.compile(
    r"\b(cutting[- ]edge|world[- ]class|synergy|leverage my|passionate|rockstar|ninja|"
    r"guru|state of the art|game[- ]changer|seamless(ly)?|robust solution)\b",
    re.IGNORECASE,
)


def _deterministic_issues(draft: ProposalDraft, spec: JobSpec | None, max_words: int, min_words: int) -> list[CritiqueIssue]:
    issues: list[CritiqueIssue] = []
    letter = (draft.cover_letter or "").strip()
    lowered = letter.lower()

    for opening in GENERIC_OPENINGS:
        if lowered.startswith(opening) or f" {opening}" in lowered[:200]:
            issues.append(
                CritiqueIssue(
                    kind="generic_opening",
                    detail=(
                        f"The opening uses '{opening}...', which is the strongest negative "
                        "signal in the outcome data. Replace it with the client's own problem."
                    ),
                    span=letter[:120],
                    severity="major",
                )
            )
            break

    if draft.word_count > max_words:
        issues.append(
            CritiqueIssue(kind="too_long",
                          detail=f"{draft.word_count} words against a {max_words}-word limit.",
                          severity="major")
        )
    elif draft.word_count < min_words:
        issues.append(
            CritiqueIssue(kind="too_short",
                          detail=f"Only {draft.word_count} words; under the {min_words}-word floor.",
                          severity="major")
        )

    if "?" not in letter:
        issues.append(
            CritiqueIssue(kind="unclear_next_step",
                          detail="No question is asked, so there is nothing easy for the client to reply to.",
                          severity="minor")
        )

    marketing = MARKETING_WORDS.findall(letter)
    if marketing:
        issues.append(
            CritiqueIssue(kind="tone",
                          detail=f"Marketing register: {', '.join(sorted({m[0] if isinstance(m, tuple) else m for m in marketing}))[:120]}.",
                          severity="minor")
        )

    if spec:
        unanswered = [a for a in draft.screening_answers if not a.answer.strip()]
        if unanswered:
            issues.append(
                CritiqueIssue(kind="missing_requirement",
                              detail=f"{len(unanswered)} screening question(s) left unanswered.",
                              severity="blocking")
            )
    if not draft.evidence_ids:
        issues.append(
            CritiqueIssue(kind="weak_personalisation",
                          detail="No portfolio evidence is cited anywhere in the draft.",
                          severity="major")
        )
    return issues


@node("critic", kind="proposal")
async def critique_draft(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    cfg = get_config()
    draft: ProposalDraft | None = state.get("draft")
    spec: JobSpec | None = state.get("job_spec")
    job = state.get("job") or {}
    revision = int(state.get("revision_count", 0))
    max_revisions = int(cfg.get("budgets.max_revisions", 2))
    max_words = int(cfg.get("policy.proposal_limits.max_words", 320))
    min_words = int(cfg.get("policy.proposal_limits.min_words", 90))

    if draft is None:
        return {"errors": ["critic: no draft to review"]}

    deterministic = _deterministic_issues(draft, spec, max_words, min_words)

    answers = "\n".join(f"Q: {a.question}\nA: {a.answer}" for a in draft.screening_answers)
    user = (
        f"CLIENT'S POST:\n{(job.get('description') or '')[:1200]}\n\n"
        f"REQUIREMENTS THE CLIENT EMPHASISED:\n"
        + "\n".join(f"- {r}" for r in (spec.requirements if spec else [])[:8])
        + f"\n\nDRAFT COVER LETTER ({draft.word_count} words, limit {max_words}):\n{draft.cover_letter}\n\n"
        f"SCREENING ANSWERS:\n{answers or '(none)'}\n\n"
        f"EVIDENCE CITED: {', '.join(draft.evidence_ids) or '(none)'}\n\n"
        f"This is revision {revision + 1} of at most {max_revisions + 1}.\n"
        f"Checks already failed deterministically: "
        + (", ".join(f"{i.kind}" for i in deterministic) or "none")
    )

    try:
        critique, _ = await llm().structured(
            "critic", [system_message("critic"), {"role": "user", "content": user}],
            Critique, ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001
        critique = Critique(
            approved=not any(i.severity in {"blocking", "major"} for i in deterministic),
            score=0.5,
            revision_instructions=f"Model unavailable ({type(exc).__name__}); deterministic checks only.",
        )

    critique.job_id = job.get("job_id", "")
    critique.revision_number = revision

    # Merge, de-duplicating by kind and keeping the stricter severity.
    merged: dict[str, CritiqueIssue] = {}
    order = {"minor": 0, "major": 1, "blocking": 2}
    for issue in list(critique.issues) + deterministic:
        existing = merged.get(issue.kind)
        if existing is None or order[issue.severity] > order[existing.severity]:
            merged[issue.kind] = issue
    critique.issues = sorted(merged.values(), key=lambda i: -order[i.severity])

    blocking_or_major = [i for i in critique.issues if i.severity in {"blocking", "major"}]
    final_pass = revision >= max_revisions

    if final_pass:
        # Approve unless genuinely blocking, and surface what remains.
        blocking = [i for i in critique.issues if i.severity == "blocking"]
        critique.approved = not blocking
        if blocking_or_major:
            critique.revision_instructions = (
                f"Revision limit ({max_revisions}) reached. "
                f"{len(blocking_or_major)} issue(s) remain for the user to decide on: "
                + "; ".join(f"{i.kind}" for i in blocking_or_major)
            )
    else:
        critique.approved = not blocking_or_major

    if not critique.score:
        penalty = sum({"minor": 0.05, "major": 0.18, "blocking": 0.4}[i.severity] for i in critique.issues)
        critique.score = max(0.0, 1.0 - penalty)

    update: dict[str, Any] = {"critique": critique, "status": "critiquing"}
    if not critique.approved:
        update["revision_count"] = revision + 1
    return update
