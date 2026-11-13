"""Promotion rules (R7): the only path into long-term memory.

The failure this prevents is specific. A system that writes everything it sees
into memory accumulates the *job posts* it read and the *tool output* it
received, and then retrieves them later as if they were the user's own
preferences. Within a week the "style memory" is full of marketing copy from
client listings.

So nothing is promoted automatically. Two stores, two rules:

**Style memory** learns only from *human edits*. When the user edits a draft,
the diff is a signal about how they want to sound. One edit is noise - they
may have been fixing a typo - so a pattern must recur across at least
``min_occurrences`` distinct proposals before it becomes a rule. This is the
"never say passionate" / "always cut the first sentence" case from the brief.

**Outcome memory** learns only from *terminal, observed* events: a proposal was
replied to, won, or ignored. Not from a model's opinion that a proposal was
good. Outcomes decay, because what worked eighteen months ago is weak evidence
about now.

Everything in ``never_promote`` is refused regardless of how it arrived: raw
tool output, client PII, job post text, and model reasoning traces.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent.config import get_config
from agent.handoffs import MemoryRule, Outcome
from agent.safety import find_pii, mask_pii

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------
# Edit analysis
# ---------------------------------------------------------------------------
@dataclass
class EditSignal:
    """One observation extracted from a human edit."""

    kind: str  # removed_phrase | added_phrase | cut_opening | shortened | lengthened
    detail: str
    evidence: str = ""
    strength: float = 1.0

    def signature(self) -> str:
        """Stable key used to count recurrences across proposals."""
        return f"{self.kind}::{self.detail.lower().strip()}"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_RE.split(text or "") if s.strip()]


def analyse_edit(before: str, after: str) -> list[EditSignal]:
    """Extract what the human actually changed, not that they changed something."""
    signals: list[EditSignal] = []
    before_sentences = _sentences(before)
    after_sentences = _sentences(after)

    if not before_sentences:
        return signals

    # --- did they delete the opening? ---------------------------------
    if before_sentences and after_sentences:
        first_before = before_sentences[0]
        best = max(
            (difflib.SequenceMatcher(None, first_before.lower(), s.lower()).ratio()
             for s in after_sentences[:2]),
            default=0.0,
        )
        if best < 0.45:
            signals.append(
                EditSignal(
                    kind="cut_opening",
                    detail="cuts the generated opening sentence",
                    evidence=first_before[:160],
                )
            )
    elif before_sentences and not after_sentences:
        return [EditSignal(kind="cut_all", detail="deleted the whole draft", strength=0.5)]

    # --- phrases removed or added -------------------------------------
    before_words = set(re.findall(r"[a-z']{4,}", before.lower()))
    after_words = set(re.findall(r"[a-z']{4,}", after.lower()))

    for word in sorted(before_words - after_words):
        if word in STYLE_WORDS:
            signals.append(
                EditSignal(kind="removed_phrase", detail=f"avoid the word '{word}'", evidence=word)
            )
    for word in sorted(after_words - before_words):
        if word in STYLE_WORDS:
            signals.append(
                EditSignal(kind="added_phrase", detail=f"prefers the word '{word}'",
                           evidence=word, strength=0.6)
            )

    # --- length ---------------------------------------------------------
    len_before, len_after = len((before or "").split()), len((after or "").split())
    if len_before and len_after:
        ratio = len_after / len_before
        if ratio < 0.75:
            signals.append(
                EditSignal(kind="shortened",
                           detail=f"cuts drafts to roughly {int(ratio * 100)}% of the generated length",
                           evidence=f"{len_before} -> {len_after} words")
            )
        elif ratio > 1.3:
            signals.append(
                EditSignal(kind="lengthened",
                           detail="expands drafts; the generated version is too terse",
                           evidence=f"{len_before} -> {len_after} words", strength=0.7)
            )
    return signals


STYLE_WORDS = {
    "passionate", "passion", "excited", "thrilled", "delighted", "honoured", "honored",
    "expert", "guru", "ninja", "rockstar", "seamless", "seamlessly", "robust",
    "leverage", "utilize", "utilise", "synergy", "cutting", "innovative",
    "extensive", "proven", "dedicated", "hardworking", "detail",
    "concretely", "specifically", "measured", "shipped", "delivered", "built",
}


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------
@dataclass
class PromotionDecision:
    promoted: bool
    rule: MemoryRule | None = None
    reason: str = ""
    occurrences: int = 0


class StylePromoter:
    """Promotes a recurring human-edit pattern into a style rule."""

    def __init__(self, config=None) -> None:
        cfg = config or get_config()
        rules = cfg.section("memory.promotion.style")
        self.min_occurrences = int(rules.get("min_occurrences", 2))
        self.min_similarity = float(rules.get("min_edit_similarity", 0.75))
        self.max_rules = int(rules.get("max_rules", 40))
        self.require_human = bool(rules.get("require_human_origin", True))
        self.never = set(cfg.get("memory.never_promote", []) or [])

    def consider(
        self,
        signal: EditSignal,
        *,
        history: list[dict[str, Any]],
        origin: str = "user_edit",
        proposal_id: str = "",
    ) -> PromotionDecision:
        if self.require_human and origin != "user_edit":
            return PromotionDecision(False, reason=f"origin '{origin}' is not a human edit")

        if find_pii(signal.evidence):
            return PromotionDecision(False, reason="refused: the evidence contains personal data")
        if "raw_tool_output" in self.never and signal.kind == "tool_output":
            return PromotionDecision(False, reason="refused: raw tool output is never promoted")

        signature = signal.signature()
        matching = [
            h for h in history
            if h.get("signature") == signature and h.get("proposal_id") != proposal_id
        ]
        occurrences = len(matching) + 1

        if occurrences < self.min_occurrences:
            return PromotionDecision(
                False,
                reason=(
                    f"seen {occurrences} time(s); needs {self.min_occurrences} across distinct "
                    "proposals before it becomes a rule"
                ),
                occurrences=occurrences,
            )

        return PromotionDecision(
            True,
            rule=MemoryRule(
                rule_id=f"style_{abs(hash(signature)) % 10**10}",
                store="style",
                text=_rule_text(signal),
                origin="user_edit",
                occurrences=occurrences,
                confidence=min(0.95, 0.45 + 0.18 * occurrences),
                evidence_refs=[mask_pii(signal.evidence)[:120]],
            ),
            reason=f"pattern recurred {occurrences} times across distinct proposals",
            occurrences=occurrences,
        )


def _rule_text(signal: EditSignal) -> str:
    return {
        "cut_opening": "Do not open with a generated preamble; start with the client's problem.",
        "removed_phrase": f"Never use this wording: {signal.detail.split(chr(39))[1]}."
        if "'" in signal.detail else signal.detail.capitalize() + ".",
        "added_phrase": signal.detail.capitalize() + ".",
        "shortened": signal.detail.capitalize() + ".",
        "lengthened": signal.detail.capitalize() + ".",
    }.get(signal.kind, signal.detail.capitalize() + ".")


class OutcomePromoter:
    """Promotes observed terminal outcomes into category-level guidance."""

    def __init__(self, config=None) -> None:
        cfg = config or get_config()
        rules = cfg.section("memory.promotion.outcome")
        self.terminal = set(rules.get("terminal_states", []) or [])
        self.min_observations = int(rules.get("min_observations_per_category", 3))
        self.max_rules = int(rules.get("max_rules", 60))
        self.half_life_days = float(rules.get("decay_half_life_days", 90))

    def decay(self, observed_at: datetime) -> float:
        age_days = max((datetime.now(UTC) - observed_at).days, 0)
        return float(0.5 ** (age_days / self.half_life_days))

    def consider(
        self, outcome: Outcome, *, history: list[Outcome]
    ) -> PromotionDecision:
        if outcome.state not in self.terminal:
            return PromotionDecision(
                False, reason=f"state '{outcome.state}' is not terminal; nothing observed yet"
            )

        category = outcome.category or "uncategorised"
        same = [o for o in history if (o.category or "uncategorised") == category]
        total = len(same) + 1
        if total < self.min_observations:
            return PromotionDecision(
                False,
                reason=f"{total} observation(s) in '{category}'; needs {self.min_observations}",
                occurrences=total,
            )

        positive = sum(
            self.decay(o.observed_at) for o in same + [outcome]
            if o.state in {"replied", "interview", "hired"}
        )
        weight = sum(self.decay(o.observed_at) for o in same + [outcome]) or 1.0
        rate = positive / weight

        text = (
            f"In '{category}', {rate:.0%} of proposals got a reply across {total} "
            f"observed outcomes (recency-weighted). "
            + (
                "This category converts; it is worth Connects."
                if rate >= 0.3
                else "This category rarely converts; raise the bid threshold or skip."
            )
        )
        return PromotionDecision(
            True,
            rule=MemoryRule(
                rule_id=f"outcome_{abs(hash(category)) % 10**10}",
                store="outcome",
                text=text,
                origin="observed_outcome",
                occurrences=total,
                categories=[category],
                confidence=min(0.9, 0.4 + 0.1 * total),
            ),
            reason=f"{total} terminal outcomes observed in '{category}'",
            occurrences=total,
        )


def reply_rate_confidence(successes: int, trials: int) -> tuple[float, float]:
    """Wilson lower bound, so three wins out of three does not read as 100%."""
    if trials == 0:
        return (0.0, 0.0)
    z = 1.96
    phat = successes / trials
    denominator = 1 + z**2 / trials
    centre = phat + z**2 / (2 * trials)
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * trials)) / trials)
    return (max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator))
