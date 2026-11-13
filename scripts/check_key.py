"""Verify the configured LLM endpoint actually answers.

Run this after setting OPEN_LLM_API_KEY. It makes one tiny call and reports
the model, latency and token usage, so a misconfigured key fails here in two
seconds rather than silently falling back to the offline stub halfway through
an evaluation.
"""

from __future__ import annotations

import asyncio
import sys

from agent.llm import get_llm_client, provider_banner


async def main() -> int:
    client = get_llm_client()
    print(provider_banner())

    if client.is_offline:
        print(
            "\nNo API key detected.\n"
            "  1. cp .env.example .env\n"
            "  2. set OPEN_LLM_BASE_URL=https://api.groq.com/openai/v1\n"
            "  3. set OPEN_LLM_API_KEY=gsk_...\n"
            "Then re-run this script."
        )
        return 1

    try:
        result = await client.complete(
            "scout",
            [{"role": "user", "content": "Reply with the single word: ready"}],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: {type(exc).__name__}: {exc}")
        print("Check the base URL and that the key is live.")
        return 1

    print(
        f"\nOK  model={result.model}  latency={result.latency_s:.2f}s  "
        f"tokens={result.usage.total}  reply={result.text.strip()[:40]!r}"
    )
    print("\nModel-dependent evaluations will now produce real measurements.")
    await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
