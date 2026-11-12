"""Layer 2b: a purpose-built injection classifier.

Why this exists
---------------
The held-out measurement in ``evals/results/injection_defence.json`` is the
reason. Hand-written patterns scored 8/8 on the attacks they were written
against, 2/8 on blind paraphrases, and **0/8** on a set written afterwards and
never tuned against. Keyword matching detects what it has already seen and
essentially nothing else.

Meta's Prompt Guard is a small encoder trained on the task itself, so it
generalises on phrasing in the way a regex cannot. It is served here through
the same OpenAI-compatible endpoint as every other open model, so it needs no
new dependency, no GPU and no second provider.

What it does NOT change
-----------------------
It is still a detector, and a detector is still layer 2. A classifier with a
good F1 is not a guarantee; it is a better prior. **Capability denial remains
the defence** - no analysis agent holds a write tool, and the one that does
refuses without a human approval token. Prompt Guard reduces how often an
attack reaches a model at all. It does not make a landed attack safe, and the
report says so.

Degradation is deliberate: if the classifier is unavailable, unauthenticated or
slow, this returns ``available=False`` and the caller falls back to the pattern
scanner rather than failing open or blocking the run.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_MODEL = "meta-llama/llama-prompt-guard-2-86m"

# Prompt Guard 2 returns a PROBABILITY of the input being an injection, as a
# bare float in the completion body - not a label. An earlier version of this
# file treated the reply as a label and looked for "1"/"0" substrings, which
# matched the digits inside "0.00080..." and flagged almost everything. The
# score is parsed as a number and compared against a threshold.
#
# The threshold is the operating point and it is a real trade: the model is
# noticeably trigger-happy on ordinary job-post English (see the benign column
# in evals/results/injection_defence.json), so a low threshold would make the
# product unusable. 0.5 is the default; PROMPT_GUARD_THRESHOLD overrides it,
# and the evaluation sweeps it.
DEFAULT_THRESHOLD = 0.5
FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


@dataclass(slots=True)
class GuardVerdict:
    available: bool
    flagged: bool = False
    score: float = 0.0
    label: str = ""
    latency_s: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "flagged": self.flagged,
            "score": round(self.score, 3),
            "label": self.label,
            "latency_s": round(self.latency_s, 4),
            "detail": self.detail[:200],
        }


class PromptGuard:
    """Thin client over an OpenAI-compatible endpoint serving Prompt Guard."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 10.0,
        threshold: float | None = None,
    ) -> None:
        self.model = model or os.getenv("PROMPT_GUARD_MODEL", DEFAULT_MODEL)
        self.threshold = (
            threshold if threshold is not None
            else float(os.getenv("PROMPT_GUARD_THRESHOLD", DEFAULT_THRESHOLD))
        )
        self.base_url = (base_url or os.getenv("OPEN_LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("OPEN_LLM_API_KEY", "")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._disabled = False

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and not self._disabled)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def classify(self, text: str) -> GuardVerdict:
        """Classify one untrusted fragment. Never raises."""
        if not self.configured or not text.strip():
            return GuardVerdict(available=False, detail="not configured")

        import time

        started = time.perf_counter()
        try:
            client = await self._http()
            response = await client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": text[:4000]}],
                    "max_tokens": 16,
                    "temperature": 0.0,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return GuardVerdict(
                available=False,
                latency_s=time.perf_counter() - started,
                detail=f"{type(exc).__name__}: {exc}",
            )

        latency = time.perf_counter() - started
        if response.status_code == 404:
            # The account does not serve this model; stop trying for the run.
            self._disabled = True
            return GuardVerdict(available=False, latency_s=latency, detail="model not available")
        if response.status_code >= 400:
            return GuardVerdict(
                available=False, latency_s=latency, detail=f"HTTP {response.status_code}"
            )

        try:
            raw = (response.json()["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return GuardVerdict(available=False, latency_s=latency, detail=f"malformed: {exc}")

        match = FLOAT_RE.search(raw)
        if match is None:
            # Not a score. Fall back to a label reading rather than guessing.
            lowered = raw.lower()
            flagged = any(t in lowered for t in ("jailbreak", "injection", "malicious", "unsafe"))
            return GuardVerdict(
                available=True, flagged=flagged, score=0.9 if flagged else 0.05,
                label=raw[:40], latency_s=latency, detail="label-mode fallback",
            )

        try:
            score = float(match.group(0))
        except ValueError:
            return GuardVerdict(available=False, latency_s=latency, detail=f"unparsable: {raw[:40]}")

        return GuardVerdict(
            available=True,
            flagged=score >= self.threshold,
            score=score,
            label=raw[:40],
            latency_s=latency,
        )

    async def classify_many(self, texts: list[str], *, concurrency: int = 8) -> list[GuardVerdict]:
        semaphore = asyncio.Semaphore(concurrency)

        async def one(text: str) -> GuardVerdict:
            async with semaphore:
                return await self.classify(text)

        return await asyncio.gather(*(one(t) for t in texts))


_GUARD: PromptGuard | None = None


def get_prompt_guard() -> PromptGuard:
    global _GUARD
    if _GUARD is None:
        _GUARD = PromptGuard()
    return _GUARD


def reset_prompt_guard() -> None:
    global _GUARD
    _GUARD = None
