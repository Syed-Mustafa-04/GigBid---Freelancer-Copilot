"""Provider-agnostic LLM access with per-node routing, budgets and accounting.

Design notes
------------
* One transport: the OpenAI-compatible ``/chat/completions`` shape. That single
  surface covers Groq, OpenRouter, Together, Ollama (``/v1``) and vLLM, so the
  "open" profile can move between a laptop and a free GPU-less host by changing
  two environment variables and nothing else.
* Every call is routed by *node name*, never by a hard-coded model string. The
  routing table lives in ``agent/config.yaml``.
* Every call returns a :class:`LLMResult` carrying tokens, latency and USD cost.
  Those are what the budget enforcement and the E5 report are computed from.
* An ``offline`` provider produces deterministic, schema-valid output. It exists
  so that unit tests, CI and a viva demo on a dead network still exercise the
  real graph rather than being skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from agent.config import Config, get_config

TModel = TypeVar("TModel", bound=BaseModel)

JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Results and accounting
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    node: str
    usage: Usage
    latency_s: float
    cost_usd: float
    provider: str
    parsed: Any = None
    repaired: bool = False
    fell_back: bool = False

    def to_trace(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "model": self.model,
            "provider": self.provider,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "latency_s": round(self.latency_s, 4),
            "cost_usd": round(self.cost_usd, 8),
            "repaired": self.repaired,
            "fell_back": self.fell_back,
        }


class BudgetExceeded(RuntimeError):
    """Raised when a thread exceeds its token or cost budget (C4)."""


class TokenBucket:
    """Client-side throttle so we stay under the provider quota.

    Reacting to 429s is necessary but not sufficient. On a free tier the
    limit is low enough that a graph fanning out three analyses and then
    drafting will blow through it in seconds, and the retry loop then spends
    its whole budget waiting - which is how a complete analysis ended up with
    no proposal attached. Pacing requests before they are sent is what makes
    the system usable on the tier this project is explicitly built for.

    Limits both requests and tokens per minute, because providers enforce
    both and the token limit is usually the one that bites first.
    """

    def __init__(self, requests_per_minute: int, tokens_per_minute: int) -> None:
        self.rpm = max(1, requests_per_minute)
        self.tpm = max(1000, tokens_per_minute)
        self._requests: list[float] = []
        self._tokens: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        self._requests = [t for t in self._requests if now - t < 60.0]
        self._tokens = [(t, n) for t, n in self._tokens if now - t < 60.0]

    async def acquire(self, estimated_tokens: int) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                spent = sum(n for _, n in self._tokens)
                if len(self._requests) < self.rpm and spent + estimated_tokens <= self.tpm:
                    self._requests.append(now)
                    self._tokens.append((now, estimated_tokens))
                    return
                oldest = min(
                    [t for t in self._requests] + [t for t, _ in self._tokens], default=now
                )
                wait = max(0.05, 60.0 - (now - oldest))
            await asyncio.sleep(min(wait, 5.0))

    def record_actual(self, estimated: int, actual: int) -> None:
        """Correct the reservation once real usage is known."""
        if not self._tokens or actual == estimated:
            return
        timestamp, _ = self._tokens[-1]
        self._tokens[-1] = (timestamp, actual)


class ProviderRateLimited(RuntimeError):
    """The provider returned 429.

    Kept distinct from a generic failure because the correct response is
    different: wait for the stated interval and retry, rather than backing off
    exponentially and giving up after three attempts. Free tiers - which this
    project is explicitly built to run on - rate-limit aggressively, and
    treating that as an error produced evaluation runs that silently lost a
    third of their rows.
    """

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(f"rate limited, retry after {retry_after:.1f}s: {detail}")
        self.retry_after = retry_after


@dataclass
class RunLedger:
    """Accumulates spend for one graph thread so budgets are enforceable."""

    max_tokens: int = 10**9
    max_usd: float = 10**9
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, result: LLMResult) -> None:
        self.usage = self.usage + result.usage
        self.cost_usd += result.cost_usd
        self.calls.append(result.to_trace())

    def check(self) -> None:
        if self.usage.total > self.max_tokens:
            raise BudgetExceeded(
                f"token budget exceeded: {self.usage.total} > {self.max_tokens}"
            )
        if self.cost_usd > self.max_usd:
            raise BudgetExceeded(f"cost budget exceeded: ${self.cost_usd:.4f} > ${self.max_usd:.4f}")

    def snapshot(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total,
            "cost_usd": round(self.cost_usd, 8),
            "calls": len(self.calls),
        }


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent token estimate (~4 chars/token).

    Only used when a provider omits usage. Real usage is preferred everywhere.
    """
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Offline deterministic provider
# ---------------------------------------------------------------------------
class OfflineProvider:
    """Deterministic stand-in used when no API key is configured.

    It returns schema-valid JSON derived from a hash of the prompt, so runs are
    reproducible and the full graph still executes. Numbers it produces are NOT
    meaningful and every eval artefact records ``provider: offline`` so results
    computed this way can never be mistaken for measured ones.
    """

    name = "offline"

    def _rng(self, prompt: str) -> random.Random:
        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12], 16)
        return random.Random(seed)

    # -- schema-aware synthesis -------------------------------------------
    # Pydantic emits nested models as $ref/$defs and optionals as anyOf, so a
    # naive walker produces values that fail validation. Resolving both is what
    # lets the offline provider satisfy hand-off models such as ProposalDraft.
    @staticmethod
    def _resolve(spec: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
        seen = 0
        while "$ref" in spec and seen < 10:
            ref = spec["$ref"]
            if not ref.startswith("#/"):
                break
            node: Any = root
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            if not isinstance(node, dict):
                break
            merged = {k: v for k, v in spec.items() if k != "$ref"}
            spec = {**node, **merged}
            seen += 1
        if "anyOf" in spec or "oneOf" in spec:
            options = spec.get("anyOf") or spec.get("oneOf") or []
            concrete = [o for o in options if isinstance(o, dict) and o.get("type") != "null"]
            if concrete:
                keep = {k: v for k, v in spec.items() if k not in {"anyOf", "oneOf"}}
                spec = {**OfflineProvider._resolve(concrete[0], root), **keep}
        if "allOf" in spec:
            merged: dict[str, Any] = {}
            for part in spec["allOf"]:
                if isinstance(part, dict):
                    merged.update(OfflineProvider._resolve(part, root))
            spec = {**merged, **{k: v for k, v in spec.items() if k != "allOf"}}
        return spec

    def _value_for(
        self, key: str, spec: dict[str, Any], rng: random.Random, prompt: str,
        root: dict[str, Any], depth: int = 0,
    ) -> Any:
        spec = self._resolve(spec, root)
        if depth > 6:
            return None
        if "const" in spec:
            return spec["const"]
        if "default" in spec and rng.random() < 0.35:
            return spec["default"]
        if "enum" in spec and spec["enum"]:
            return spec["enum"][rng.randrange(len(spec["enum"]))]

        kind = spec.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        if kind is None and "properties" in spec:
            kind = "object"

        if kind == "string":
            return self._string_for(key, prompt, rng, spec)
        if kind == "integer":
            lo, hi = int(spec.get("minimum", 0)), int(spec.get("maximum", 10))
            return rng.randint(lo, max(lo, hi))
        if kind == "number":
            lo = float(spec.get("minimum", 0.0))
            hi = float(spec.get("maximum", 1.0))
            if hi < lo:
                hi = lo + 1.0
            return round(rng.uniform(lo, hi), 3)
        if kind == "boolean":
            return rng.random() > 0.5
        if kind == "array":
            item_spec = spec.get("items") or {"type": "string"}
            count = rng.randint(int(spec.get("minItems", 1)), max(int(spec.get("minItems", 1)), 2))
            return [
                self._value_for(key, item_spec, rng, prompt, root, depth + 1) for _ in range(count)
            ]
        if kind == "object":
            return self._object_for(spec, rng, prompt, root, depth + 1)
        return self._string_for(key, prompt, rng, spec)

    def _string_for(
        self, key: str, prompt: str, rng: random.Random, spec: dict[str, Any] | None = None
    ) -> str:
        fmt = (spec or {}).get("format", "")
        if fmt == "date-time":
            return "2026-11-15T12:00:00+00:00"
        if fmt == "date":
            return "2026-11-15"
        if fmt == "uri":
            return "https://example.invalid/offline"
        lowered = key.lower()
        if "reason" in lowered or "explanation" in lowered or "rationale" in lowered or "note" in lowered:
            return "Deterministic offline rationale generated without a model."
        if "question" in lowered:
            return "Offline placeholder question."
        if "answer" in lowered or "letter" in lowered or "body" in lowered or "text" in lowered:
            return (
                "This is offline placeholder text. It makes no claims about experience "
                "because no model was available to ground them."
            )
        if lowered.endswith("_id") or lowered == "id":
            return f"offline-{rng.randrange(1000, 9999)}"
        return f"offline-{lowered}"

    def _object_for(
        self, schema: dict[str, Any], rng: random.Random, prompt: str,
        root: dict[str, Any] | None = None, depth: int = 0,
    ) -> dict[str, Any]:
        root = root if root is not None else schema
        schema = self._resolve(schema, root)
        props: dict[str, Any] = schema.get("properties", {})
        required = schema.get("required") or list(props.keys())
        out: dict[str, Any] = {}
        for key in required:
            spec = props.get(key, {"type": "string"})
            out[key] = self._value_for(key, spec, rng, prompt, root, depth)
        return out

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        **_: Any,
    ) -> tuple[str, Usage]:
        prompt = json.dumps(messages, sort_keys=True)
        rng = self._rng(prompt)
        await asyncio.sleep(0.001)
        if schema:
            payload = self._object_for(schema, rng, prompt, schema, 0)
            text = json.dumps(payload)
        else:
            text = self._string_for("text", prompt, rng)
        return text, Usage(estimate_tokens(prompt), estimate_tokens(text))


# ---------------------------------------------------------------------------
# OpenAI-compatible HTTP provider
# ---------------------------------------------------------------------------
class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, timeout: float = 90.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **_: Any,
    ) -> tuple[str, Usage]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            # json_object is the widely supported mode; the strict schema is
            # additionally restated in the system prompt by StructuredPrompt.
            payload["response_format"] = {"type": "json_object"}

        client = await self._get_client()
        response = await client.post("/chat/completions", json=payload)
        if response.status_code == 429:
            # Groq returns Retry-After in seconds; some providers use a date.
            # Fall back to a conservative default rather than hammering.
            header = response.headers.get("Retry-After", "")
            try:
                wait = float(header)
            except ValueError:
                wait = 8.0
            raise ProviderRateLimited(min(wait, 60.0), response.text[:200])

        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{response.status_code} from {self.base_url}: {response.text[:400]}",
                request=response.request,
                response=response,
            )
        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Malformed completion payload: {str(data)[:300]}") from exc
        raw_usage = data.get("usage") or {}
        usage = Usage(
            int(raw_usage.get("prompt_tokens", 0) or 0),
            int(raw_usage.get("completion_tokens", 0) or 0),
        )
        if usage.total == 0:
            usage = Usage(estimate_tokens(json.dumps(messages)), estimate_tokens(text))
        return text, usage


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response that may be fenced or chatty."""
    text = text.strip()
    if not text:
        raise ValueError("empty response")
    match = JSON_BLOCK.search(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost balanced object/array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in response: {text[:200]}")


class LLMClient:
    """Routes a node name to a model and returns accounted results."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self._providers: dict[str, Any] = {}
        self._offline = OfflineProvider()
        limits = self.config.section("models.rate_limit")
        self._bucket = TokenBucket(
            requests_per_minute=int(limits.get("requests_per_minute", 25)),
            tokens_per_minute=int(limits.get("tokens_per_minute", 14000)),
        )

    # -- provider resolution ----------------------------------------------
    def _primary(self) -> tuple[Any, str]:
        base_url, api_key = self.config.profile_endpoint()
        if not base_url or not api_key:
            return self._offline, "offline"
        key = f"primary::{base_url}"
        if key not in self._providers:
            self._providers[key] = OpenAICompatibleProvider(base_url, api_key)
        return self._providers[key], "openai_compatible"

    def _fallback(self) -> tuple[Any, str, str | None]:
        base_url, api_key, model = self.config.fallback_endpoint()
        if not base_url or not api_key:
            return self._offline, "offline", None
        key = f"fallback::{base_url}"
        if key not in self._providers:
            self._providers[key] = OpenAICompatibleProvider(base_url, api_key)
        return self._providers[key], "openai_compatible", model

    @property
    def is_offline(self) -> bool:
        return self._primary()[1] == "offline"

    async def aclose(self) -> None:
        for provider in self._providers.values():
            if hasattr(provider, "aclose"):
                await provider.aclose()

    # -- cost ---------------------------------------------------------------
    def _cost(self, model: str, usage: Usage) -> float:
        price = self.config.price_for(model)
        return (
            usage.prompt_tokens * price["input"] + usage.completion_tokens * price["output"]
        ) / 1_000_000

    # -- core call ----------------------------------------------------------
    async def complete(
        self,
        node: str,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        ledger: RunLedger | None = None,
        max_attempts: int = 3,
    ) -> LLMResult:
        spec = self.config.node_model(node)
        model = spec["model"]
        temperature = float(spec.get("temperature", 0.0))
        max_tokens = int(spec.get("max_tokens", 1024))

        provider, provider_name = self._primary()
        fell_back = False
        last_error: Exception | None = None
        rate_limited = 0
        max_rate_limit_waits = 8

        attempt = -1
        while True:
            attempt += 1
            if attempt >= max_attempts:
                break
            # Pace before sending, not after being refused.
            #
            # Reserve the prompt plus a FRACTION of the output allowance, not
            # the whole of it. `max_tokens` is a ceiling the model almost never
            # reaches - a 1,600-token allowance typically produces ~300 - and
            # booking the ceiling for three parallel analyses reserved more
            # than the entire per-minute budget, so the fan-out serialised
            # behind a 60-second wait. The reservation is corrected to actual
            # usage as soon as the response arrives.
            if provider_name != "offline":
                reservation = estimate_tokens(json.dumps(messages)) + max(
                    128, int(max_tokens * 0.3)
                )
                await self._bucket.acquire(reservation)
            started = time.perf_counter()
            try:
                text, usage = await provider.complete(
                    model=model,
                    messages=messages,
                    schema=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                latency = time.perf_counter() - started
                if provider_name != "offline":
                    self._bucket.record_actual(reservation, usage.total)
                result = LLMResult(
                    text=text,
                    model=model,
                    node=node,
                    usage=usage,
                    latency_s=latency,
                    cost_usd=self._cost(model, usage),
                    provider=provider_name,
                    fell_back=fell_back,
                )
                if ledger is not None:
                    ledger.record(result)
                    ledger.check()
                return result
            except BudgetExceeded:
                raise
            except ProviderRateLimited as exc:
                # Being throttled is not a failure of the call. The loop
                # increments `attempt` at the top, so the counter is rolled
                # back here - without this the first three 429s exhausted the
                # retry budget and the node failed, which is exactly the bug
                # this handler was added to prevent.
                last_error = exc
                rate_limited += 1
                attempt -= 1
                if rate_limited > max_rate_limit_waits:
                    raise
                await asyncio.sleep(exc.retry_after + 0.25)
                continue
            except Exception as exc:  # noqa: BLE001 - provider errors are varied
                last_error = exc
                # On the final primary attempt, try the configured fallback once (C7).
                if attempt == max_attempts - 2 and not fell_back:
                    fb_provider, fb_name, fb_model = self._fallback()
                    if fb_model:
                        provider, provider_name, model = fb_provider, fb_name, fb_model
                        fell_back = True
                        continue
                if attempt < max_attempts - 1:
                    await asyncio.sleep(min(0.5 * (2**attempt), 4.0))

        raise RuntimeError(f"LLM call for node '{node}' failed after {max_attempts} attempts: {last_error}")

    async def structured(
        self,
        node: str,
        messages: list[dict[str, str]],
        model_cls: type[TModel],
        *,
        ledger: RunLedger | None = None,
    ) -> tuple[TModel, LLMResult]:
        """Call the model and validate into ``model_cls``, repairing once."""
        schema = model_cls.model_json_schema()
        schema_text = json.dumps(schema, indent=2)
        primed = list(messages)
        primed.append(
            {
                "role": "system",
                "content": (
                    "Respond with a single JSON object and nothing else. "
                    "It must validate against this JSON Schema:\n" + schema_text
                ),
            }
        )

        result = await self.complete(node, primed, schema=schema, ledger=ledger)
        try:
            parsed = model_cls.model_validate(_extract_json(result.text))
            result.parsed = parsed
            return parsed, result
        except (ValidationError, ValueError) as first_error:
            repair = primed + [
                {"role": "assistant", "content": result.text[:2000]},
                {
                    "role": "user",
                    "content": (
                        "That response did not validate. Error:\n"
                        f"{str(first_error)[:800]}\n\n"
                        "Return ONLY the corrected JSON object."
                    ),
                },
            ]
            retry = await self.complete(node, repair, schema=schema, ledger=ledger)
            retry.repaired = True
            parsed = model_cls.model_validate(_extract_json(retry.text))
            retry.parsed = parsed
            return parsed, retry


_CLIENT: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient()
    return _CLIENT


def reset_llm_client() -> None:
    global _CLIENT
    _CLIENT = None


def provider_banner() -> str:
    """One line describing what will actually answer, for logs and the UI."""
    client = get_llm_client()
    if client.is_offline:
        return (
            "LLM provider: OFFLINE deterministic stub (no OPEN_LLM_API_KEY set). "
            "The graph runs end to end but numbers are not measurements."
        )
    base = os.getenv(
        client.config.get(f"models.profiles.{client.config.profile_name}.base_url_env", "")
    )
    return f"LLM provider: {base} (profile={client.config.profile_name})"
