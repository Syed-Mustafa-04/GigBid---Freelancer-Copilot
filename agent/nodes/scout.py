"""Scout: run saved searches on schedule, de-duplicate, queue new jobs.

Runs in the worker, not in a web request. Deliberately cheap - its only job is
to avoid waking the expensive analysts on something they have already seen, so
it uses deterministic de-duplication rather than a model.

De-duplication is the part worth getting right. A reposted job is the same job
with a new id and a later timestamp, and analysing it again wastes tokens and
shows the user a duplicate card. Matching on id alone misses every repost, so
a normalised title + client fingerprint is used alongside it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from agent.config import get_config
from agent.mcp_client import ToolUnavailable, get_registry

WORD_RE = re.compile(r"[a-z0-9]+")


def fingerprint(job: dict[str, Any]) -> str:
    """Stable identity for a job across reposts.

    Title words (sorted, stopword-free) + client + budget band. Reposts keep
    the text and the client; what changes is the id and the timestamp.
    """
    title_words = sorted(set(WORD_RE.findall((job.get("title") or "").lower())))
    budget = job.get("budget") or {}
    band = int(float(budget.get("max") or 0) // 250)
    raw = f"{' '.join(title_words)}|{job.get('client_id','')}|{band}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ScoutResult:
    queued: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    degraded: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "queued": len(self.queued),
            "duplicates": len(self.duplicates),
            "dropped": len(self.dropped),
            "errors": self.errors,
            "degraded": self.degraded,
        }


OFF_DOMAIN = {
    "translation", "voice acting", "voice-over", "rigging", "colour grading",
    "color grading", "telemarketing", "cold calling", "autocad", "civil engineering",
    "wedding", "mandarin", "premiere pro", "blender",
}


class Scout:
    """Stateful across runs so de-duplication survives a worker restart."""

    def __init__(self, seen_fingerprints: set[str] | None = None, seen_ids: set[str] | None = None) -> None:
        self.seen_fingerprints: set[str] = seen_fingerprints or set()
        self.seen_ids: set[str] = seen_ids or set()
        self.config = get_config()

    def _off_domain(self, job: dict[str, Any]) -> str | None:
        blob = f"{job.get('title','')} {' '.join(job.get('required_skills', []))}".lower()
        for term in OFF_DOMAIN:
            if term in blob:
                return term
        return None

    async def run_search(
        self,
        query: str = "",
        *,
        skills: list[str] | None = None,
        budget_min: float | None = None,
        limit: int = 25,
        posted_after: str | None = None,
    ) -> ScoutResult:
        result = ScoutResult()
        registry = get_registry()
        try:
            payload = await registry.call(
                "scout", "marketplace", "search_jobs",
                {
                    "query": query,
                    "skills": skills or [],
                    "budget_min": budget_min,
                    "limit": limit,
                    "posted_after": posted_after,
                },
            )
        except ToolUnavailable as exc:
            result.degraded = True
            result.errors.append(exc.user_message)
            return result

        if payload.get("degraded"):
            result.degraded = True
            result.errors.append(payload.get("guidance", "marketplace unavailable"))
            return result

        for job in payload.get("jobs", []):
            job_id = job.get("job_id", "")
            if job_id in self.seen_ids:
                result.duplicates.append({"job_id": job_id, "reason": "already seen by id"})
                continue

            print_ = fingerprint(job)
            if print_ in self.seen_fingerprints:
                result.duplicates.append(
                    {"job_id": job_id, "reason": "repost of a job already seen", "fingerprint": print_}
                )
                self.seen_ids.add(job_id)
                continue

            off = self._off_domain(job)
            if off:
                result.dropped.append({"job_id": job_id, "reason": f"outside the freelancer's field ({off})"})
                self.seen_ids.add(job_id)
                self.seen_fingerprints.add(print_)
                continue

            budget = job.get("budget") or {}
            if float(budget.get("max") or 0) == 0 and len(job.get("description", "")) > 200:
                result.dropped.append(
                    {"job_id": job_id, "reason": "zero budget for a substantive scope"}
                )
                self.seen_ids.add(job_id)
                self.seen_fingerprints.add(print_)
                continue

            self.seen_ids.add(job_id)
            self.seen_fingerprints.add(print_)
            result.queued.append(job)

        return result

    async def run_saved_searches(self, searches: list[dict[str, Any]]) -> ScoutResult:
        """Run several saved searches, merging and de-duplicating across them."""
        combined = ScoutResult()
        for search in searches:
            one = await self.run_search(
                search.get("query", ""),
                skills=search.get("skills"),
                budget_min=search.get("budget_min"),
                limit=int(search.get("limit", 25)),
                posted_after=search.get("posted_after"),
            )
            combined.queued.extend(one.queued)
            combined.duplicates.extend(one.duplicates)
            combined.dropped.extend(one.dropped)
            combined.errors.extend(one.errors)
            combined.degraded = combined.degraded or one.degraded
        return combined
