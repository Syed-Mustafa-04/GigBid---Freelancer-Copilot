"""Claim splitting and grounding (B3). This is what enforces R5.

The rule GigBid must never break: *no sentence in a proposal may claim
experience the portfolio does not support.* A model asked to write persuasively
from eight fragments will reliably overstate - "extensive experience",
"I have worked with dozens of clients in your industry" - because that is what
persuasive writing looks like in its training data.

So the draft is not trusted. It is split into atomic claims, each claim is
checked against the evidence that was actually retrieved, and anything
unsupported is rewritten or deleted **before the Critic ever sees it**. Order
matters: the Critic judges quality, and a fabricated sentence is often the
highest-quality-sounding one in the draft.

Three statuses:

``supported``        the evidence states it.
``partly_supported`` the evidence supports a weaker version. Rewrite to the
                     weaker version rather than delete - that is usually the
                     true sentence, and deleting it loses a real selling point.
``unsupported``      nothing backs it. Removed.

Two checkers, same interface: a deterministic lexical one (default, free,
runs in CI) and an LLM entailment one (more accurate on paraphrase). The
reported unsupported-claim rate names which was used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent.handoffs import Claim, ClaimStatus, ClaimVerification, EvidenceItem, ProposalDraft

# Sentences that assert experience, capability or results - the ones that can
# be wrong in a way that matters. A sentence proposing future work cannot be
# "unsupported" because it is not a claim about the past.
CLAIM_MARKERS = re.compile(
    r"\b(i|we)\s+(have|had|has|built|created|delivered|shipped|implemented|designed|led|managed|"
    r"worked|developed|deployed|migrated|reduced|increased|improved|cut|raised|automated|scaled|"
    r"architected|maintained|optimis|optimiz|handled|supported|ran|run)\b",
    re.IGNORECASE,
)
EXPERIENCE_WORDS = re.compile(
    r"\b(years?\s+of\s+experience|extensive|expert|specialist|proven track record|"
    r"dozens|hundreds|many clients|always|every time|guarantee)\b",
    re.IGNORECASE,
)
FUTURE_MARKERS = re.compile(
    r"\b(i\s+would|i\s+will|i\s+can|my\s+approach|the\s+plan|first\s+week|next\s+step|"
    r"i\s+propose|happy\s+to|let\s+me\s+know|one\s+question)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|x|k|hours?|days?|weeks?|months?|years?)?\b")
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had", "was", "were",
    "our", "your", "their", "its", "but", "not", "you", "are", "can", "will", "would", "about",
    "into", "over", "under", "they", "them", "then", "than", "when", "what", "which", "who",
}


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if len(p.strip()) > 12]


def is_claim(sentence: str) -> bool:
    """A sentence is checkable if it asserts something about the past."""
    if EXPERIENCE_WORDS.search(sentence):
        return True
    if not CLAIM_MARKERS.search(sentence):
        return False
    # "I would have built X" is a proposal, not a claim.
    if FUTURE_MARKERS.search(sentence) and not re.search(
        r"\b(i|we)\s+(built|delivered|shipped|implemented|reduced|increased|cut|led)\b",
        sentence,
        re.IGNORECASE,
    ):
        return False
    return True


def _content_terms(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9][a-z0-9\-\.]+", text.lower())
        if len(w) > 3 and w not in STOPWORDS
    }


@dataclass
class ClaimCheck:
    status: ClaimStatus
    score: float
    evidence_ids: list[str]
    note: str


class LexicalGrounder:
    """Deterministic grounding check. Free, reproducible, runs in CI."""

    name = "lexical"

    def __init__(self, supported_at: float = 0.60, partial_at: float = 0.34) -> None:
        self.supported_at = supported_at
        self.partial_at = partial_at

    def check(self, claim_text: str, evidence: list[EvidenceItem]) -> ClaimCheck:
        terms = _content_terms(claim_text)
        if not terms:
            return ClaimCheck(ClaimStatus.SUPPORTED, 1.0, [], "no checkable content")

        scored: list[tuple[float, str]] = []
        for item in evidence:
            blob = _content_terms(f"{item.title} {item.text} {' '.join(item.skills)}")
            if not blob:
                continue
            overlap = len(terms & blob) / len(terms)
            scored.append((overlap, item.evidence_id))
        scored.sort(reverse=True)

        best = scored[0][0] if scored else 0.0
        # Two items each covering half the claim do support it together.
        covered: set[str] = set()
        contributors: list[str] = []
        for overlap, eid in scored[:4]:
            if overlap <= 0.05:
                continue
            item = next((e for e in evidence if e.evidence_id == eid), None)
            if item is None:
                continue
            before = len(covered)
            covered |= terms & _content_terms(f"{item.title} {item.text} {' '.join(item.skills)}")
            if len(covered) > before:
                contributors.append(eid)
        union = len(covered) / len(terms)
        score = max(best, union * 0.9)

        # A number in the claim must appear in the evidence. Invented metrics
        # are the most damaging failure, so they are checked explicitly.
        claim_numbers = set(NUMBER_RE.findall(claim_text))
        if claim_numbers:
            evidence_text = " ".join(f"{e.title} {e.text}" for e in evidence)
            evidence_numbers = set(NUMBER_RE.findall(evidence_text))
            if not (claim_numbers & evidence_numbers):
                return ClaimCheck(
                    ClaimStatus.UNSUPPORTED,
                    min(score, 0.25),
                    contributors[:2],
                    f"cites figures {sorted(claim_numbers)[:3]} that appear in no retrieved evidence",
                )

        if EXPERIENCE_WORDS.search(claim_text):
            return ClaimCheck(
                ClaimStatus.UNSUPPORTED if score < 0.75 else ClaimStatus.PARTLY_SUPPORTED,
                score,
                contributors[:2],
                "uses an unquantifiable experience claim the portfolio cannot establish",
            )

        if score >= self.supported_at:
            return ClaimCheck(ClaimStatus.SUPPORTED, score, contributors[:3], "grounded in retrieved evidence")
        if score >= self.partial_at:
            return ClaimCheck(
                ClaimStatus.PARTLY_SUPPORTED, score, contributors[:3],
                "evidence supports a narrower version of this claim",
            )
        return ClaimCheck(ClaimStatus.UNSUPPORTED, score, contributors[:1], "no retrieved evidence supports this")


class LLMGrounder:
    """Entailment check with the model. Better on paraphrase, costs a call."""

    name = "llm"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from agent.llm import get_llm_client

            self._client = get_llm_client()
        return self._client

    async def acheck(self, claim_text: str, evidence: list[EvidenceItem]) -> ClaimCheck:
        from pydantic import BaseModel, Field

        class Verdict(BaseModel):
            status: str = Field(description="supported | partly_supported | unsupported")
            evidence_ids: list[str] = Field(default_factory=list)
            reason: str = ""

        bundle = "\n\n".join(
            f"[{e.evidence_id}] {e.title}\n{e.text[:700]}" for e in evidence[:8]
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You check whether a claim in a freelance proposal is supported by the "
                    "freelancer's portfolio evidence. Be strict. A claim is 'supported' only if "
                    "the evidence states it. If the evidence supports a weaker version, say "
                    "'partly_supported'. If nothing backs it, say 'unsupported'. Cite the "
                    "evidence ids you relied on."
                ),
            },
            {"role": "user", "content": f"EVIDENCE:\n{bundle}\n\nCLAIM:\n{claim_text}"},
        ]
        try:
            verdict, _ = await self.client.structured("claim_verifier", messages, Verdict)
            status = {
                "supported": ClaimStatus.SUPPORTED,
                "partly_supported": ClaimStatus.PARTLY_SUPPORTED,
            }.get(verdict.status.strip().lower(), ClaimStatus.UNSUPPORTED)
            score = {ClaimStatus.SUPPORTED: 0.9, ClaimStatus.PARTLY_SUPPORTED: 0.5}.get(status, 0.1)
            return ClaimCheck(status, score, verdict.evidence_ids[:3], verdict.reason[:300])
        except Exception as exc:  # noqa: BLE001 - fail closed, never fail open
            return LexicalGrounder().check(claim_text, evidence)._replace_note(
                f"LLM grounder unavailable ({type(exc).__name__}); used lexical fallback"
            )


def _replace_note(self: ClaimCheck, note: str) -> ClaimCheck:
    return ClaimCheck(self.status, self.score, self.evidence_ids, note)


ClaimCheck._replace_note = _replace_note  # type: ignore[attr-defined]


def rewrite_partial(claim_text: str) -> str:
    """Weaken a partly-supported claim to the version the evidence supports."""
    text = claim_text
    softeners = [
        (r"\bextensive experience\b", "experience"),
        (r"\bexpert\b", "experienced"),
        (r"\bdozens of\b", "several"),
        (r"\bhundreds of\b", "a number of"),
        (r"\bmany clients\b", "clients"),
        (r"\balways\b", "typically"),
        (r"\bevery time\b", "consistently"),
        (r"\bI guarantee\b", "I aim"),
        (r"\bproven track record\b", "track record"),
        (r"\b(\d+)\+?\s*years? of experience\b", "experience"),
        (r"\bdeep expertise\b", "working knowledge"),
    ]
    for pattern, replacement in softeners:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


async def verify_draft(
    draft: ProposalDraft,
    evidence: list[EvidenceItem],
    *,
    grounder: Any | None = None,
    use_llm: bool = False,
) -> tuple[ProposalDraft, ClaimVerification]:
    """Split, check, rewrite or remove. Returns the cleaned draft and the report."""
    checker = grounder or (LLMGrounder() if use_llm else LexicalGrounder())
    claims: list[Claim] = []
    unsupported_before = 0

    async def run(text: str) -> ClaimCheck:
        if hasattr(checker, "acheck"):
            return await checker.acheck(text, evidence)
        return checker.check(text, evidence)

    # --- cover letter ----------------------------------------------------
    kept_sentences: list[str] = []
    for index, sentence in enumerate(split_sentences(draft.cover_letter)):
        if not is_claim(sentence):
            kept_sentences.append(sentence)
            continue
        check = await run(sentence)
        claim = Claim(
            claim_id=f"c{index}",
            text=sentence,
            location="cover_letter",
            status=check.status,
            evidence_ids=check.evidence_ids,
            support_score=round(check.score, 3),
            verifier_note=check.note,
        )
        if check.status == ClaimStatus.SUPPORTED:
            kept_sentences.append(sentence)
        elif check.status == ClaimStatus.PARTLY_SUPPORTED:
            unsupported_before += 0
            rewritten = rewrite_partial(sentence)
            claim.rewritten_as = rewritten
            kept_sentences.append(rewritten)
        else:
            unsupported_before += 1
            claim.removed = True
        claims.append(claim)

    # --- screening answers -----------------------------------------------
    cleaned_answers = []
    for answer_index, answer in enumerate(draft.screening_answers):
        kept_answer: list[str] = []
        for index, sentence in enumerate(split_sentences(answer.answer)):
            if not is_claim(sentence):
                kept_answer.append(sentence)
                continue
            check = await run(sentence)
            claim = Claim(
                claim_id=f"a{answer_index}_{index}",
                text=sentence,
                location="screening_answer",
                answer_index=answer_index,
                status=check.status,
                evidence_ids=check.evidence_ids,
                support_score=round(check.score, 3),
                verifier_note=check.note,
            )
            if check.status == ClaimStatus.SUPPORTED:
                kept_answer.append(sentence)
            elif check.status == ClaimStatus.PARTLY_SUPPORTED:
                rewritten = rewrite_partial(sentence)
                claim.rewritten_as = rewritten
                kept_answer.append(rewritten)
            else:
                unsupported_before += 1
                claim.removed = True
            claims.append(claim)
        answer.answer = " ".join(kept_answer).strip()
        cleaned_answers.append(answer)

    draft.cover_letter = " ".join(kept_sentences).strip()
    draft.screening_answers = cleaned_answers
    draft.recount()

    report = ClaimVerification.from_claims(draft.job_id, claims, unsupported_before)
    return draft, report


def unsupported_rate(report: ClaimVerification) -> dict[str, float]:
    """Before/after rates, which is what B3 asks to be reported."""
    total = len(report.claims)
    return {
        "claims_total": total,
        "unsupported_before": report.unsupported_before,
        "unsupported_after": report.unsupported_after,
        "unsupported_rate_before": round(report.unsupported_before / total, 4) if total else 0.0,
        "unsupported_rate_after": round(report.unsupported_after / total, 4) if total else 0.0,
        "support_ratio": round(report.support_ratio, 4),
        "rewritten": sum(1 for c in report.claims if c.rewritten_as),
        "removed": sum(1 for c in report.claims if c.removed),
    }
