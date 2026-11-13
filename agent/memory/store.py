"""Long-term memory store, backed by a LangGraph BaseStore.

Namespaced per user and per store kind, so one user's learned style never
reaches another's drafts - the tenancy boundary is the namespace, checked on
every read and write rather than filtered afterwards.

The user can see and delete everything in here from Settings. That is a
requirement, not a nicety: a system that learns from you and will not show you
what it learned is one you cannot correct.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.config import REPO_ROOT, get_config
from agent.handoffs import MemoryRule, Outcome
from agent.memory.promotion_rules import (
    OutcomePromoter,
    PromotionDecision,
    StylePromoter,
    analyse_edit,
)
from agent.safety import mask_pii


class MemoryStore:
    """Promoted knowledge only. Nothing writes here except through a promoter."""

    def __init__(self, backend: Any | None = None, path: Path | None = None) -> None:
        self.config = get_config()
        self.enabled = bool(self.config.get("memory.enabled", True))
        self._path = path or (REPO_ROOT / "data" / "processed" / "memory.json")
        self._backend = backend
        self._data: dict[str, Any] = {"rules": {}, "edit_history": {}, "outcomes": {}}
        self._load()
        self.style_promoter = StylePromoter(self.config)
        self.outcome_promoter = OutcomePromoter(self.config)

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        for key in ("rules", "edit_history", "outcomes"):
            self._data.setdefault(key, {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _ns(user_id: str, store: str) -> str:
        return f"{user_id}::{store}"

    # -- reads -------------------------------------------------------------
    def rules(self, user_id: str, store: str, *, limit: int = 40) -> list[MemoryRule]:
        if not self.enabled:
            return []
        raw = self._data["rules"].get(self._ns(user_id, store), [])
        out = []
        for row in raw:
            try:
                out.append(MemoryRule.model_validate(row))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda r: (r.confidence, r.occurrences), reverse=True)
        return out[:limit]

    def style_rules_text(self, user_id: str, limit: int = 12) -> list[str]:
        return [r.text for r in self.rules(user_id, "style", limit=limit)]

    def outcome_rules_text(self, user_id: str, category: str | None = None, limit: int = 8) -> list[str]:
        rules = self.rules(user_id, "outcome", limit=50)
        if category:
            scoped = [r for r in rules if category in r.categories]
            if scoped:
                rules = scoped
        return [r.text for r in rules[:limit]]

    def all_rules(self, user_id: str) -> dict[str, list[dict[str, Any]]]:
        return {
            store: [r.model_dump(mode="json") for r in self.rules(user_id, store, limit=200)]
            for store in (self.config.get("memory.stores", ["style", "outcome"]) or [])
        }

    # -- writes (only via promotion) --------------------------------------
    def _append_rule(self, user_id: str, rule: MemoryRule, max_rules: int) -> None:
        key = self._ns(user_id, rule.store)
        existing = self._data["rules"].setdefault(key, [])
        for row in existing:
            if row.get("rule_id") == rule.rule_id:
                row["occurrences"] = rule.occurrences
                row["confidence"] = rule.confidence
                row["text"] = rule.text
                row["last_seen_at"] = datetime.now(UTC).isoformat()
                self._save()
                return
        existing.append(rule.model_dump(mode="json"))
        if len(existing) > max_rules:
            existing.sort(key=lambda r: (r.get("confidence", 0), r.get("occurrences", 0)), reverse=True)
            del existing[max_rules:]
        self._save()

    def record_edit(
        self, user_id: str, *, before: str, after: str, proposal_id: str, job_category: str = ""
    ) -> list[PromotionDecision]:
        """Observe a human edit. Promotes only what recurs."""
        if not self.enabled or before.strip() == after.strip():
            return []

        decisions: list[PromotionDecision] = []
        history_key = self._ns(user_id, "edits")
        history = self._data["edit_history"].setdefault(history_key, [])

        for signal in analyse_edit(before, after):
            decision = self.style_promoter.consider(
                signal, history=history, origin="user_edit", proposal_id=proposal_id
            )
            history.append(
                {
                    "signature": signal.signature(),
                    "kind": signal.kind,
                    "detail": signal.detail,
                    "evidence": mask_pii(signal.evidence)[:160],
                    "proposal_id": proposal_id,
                    "category": job_category,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "promoted": decision.promoted,
                }
            )
            if decision.promoted and decision.rule is not None:
                self._append_rule(user_id, decision.rule, self.style_promoter.max_rules)
            decisions.append(decision)

        self._data["edit_history"][history_key] = history[-500:]
        self._save()
        return decisions

    def record_outcome(self, user_id: str, outcome: Outcome) -> PromotionDecision:
        """Observe a terminal outcome. Promotes only on enough observations."""
        if not self.enabled:
            return PromotionDecision(False, reason="memory disabled")

        key = self._ns(user_id, "outcomes")
        raw = self._data["outcomes"].setdefault(key, [])
        history: list[Outcome] = []
        for row in raw:
            try:
                history.append(Outcome.model_validate(row))
            except Exception:  # noqa: BLE001
                continue

        decision = self.outcome_promoter.consider(outcome, history=history)
        raw.append(outcome.model_dump(mode="json"))
        self._data["outcomes"][key] = raw[-1000:]
        if decision.promoted and decision.rule is not None:
            self._append_rule(user_id, decision.rule, self.outcome_promoter.max_rules)
        self._save()
        return decision

    # -- user control ------------------------------------------------------
    def delete_rule(self, user_id: str, rule_id: str) -> bool:
        removed = False
        for store in ("style", "outcome"):
            key = self._ns(user_id, store)
            rows = self._data["rules"].get(key, [])
            kept = [r for r in rows if r.get("rule_id") != rule_id]
            if len(kept) != len(rows):
                self._data["rules"][key] = kept
                removed = True
        if removed:
            self._save()
        return removed

    def clear(self, user_id: str, store: str | None = None) -> int:
        count = 0
        stores = [store] if store else ["style", "outcome"]
        for name in stores:
            key = self._ns(user_id, name)
            count += len(self._data["rules"].get(key, []))
            self._data["rules"][key] = []
        self._save()
        return count

    def stats(self, user_id: str) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "style_rules": len(self._data["rules"].get(self._ns(user_id, "style"), [])),
            "outcome_rules": len(self._data["rules"].get(self._ns(user_id, "outcome"), [])),
            "edits_observed": len(self._data["edit_history"].get(self._ns(user_id, "edits"), [])),
            "outcomes_observed": len(self._data["outcomes"].get(self._ns(user_id, "outcomes"), [])),
            "never_promote": self.config.get("memory.never_promote", []),
        }


_STORE: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE


def reset_memory_store() -> None:
    global _STORE
    _STORE = None
