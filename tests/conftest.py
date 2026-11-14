"""Shared test configuration.

Tests run against the deterministic offline providers: the hash embedder and
the offline LLM. That keeps CI free, fast and reproducible, and it means a test
failure is a code change rather than a model's mood or a provider's quota.

Isolating from `.env` is subtler than it looks. `agent/config.py` calls
`load_dotenv(..., override=False)` at import time, and `override=False` only
skips keys that are *already present* in `os.environ`. Deleting the key here
therefore does not work: config import puts it straight back, and the whole
suite starts making live API calls. Under free-tier rate limiting that turned a
30-second run into one that waited 60 seconds per throttled call.

Setting the keys to empty strings is what actually isolates: they are present,
so `load_dotenv` leaves them alone, and the LLM client treats an empty key as
"not configured" and selects the offline provider.
"""

from __future__ import annotations

import os

import pytest

# Must happen before anything imports agent.config.
os.environ["EMBEDDING_BACKEND"] = "hash"
os.environ["GIGBID__MODELS__RERANKER__BACKEND"] = "none"
os.environ["APP_SECRET_KEY"] = "test-secret-not-for-production"

# Present-but-empty, NOT absent. See the module docstring.
for _key in (
    "OPEN_LLM_API_KEY",
    "OPEN_LLM_BASE_URL",
    "OPEN_LLM_FALLBACK_API_KEY",
    "OPEN_LLM_FALLBACK_BASE_URL",
    "PAID_LLM_API_KEY",
    "CHECKPOINT_DATABASE_URL",
):
    os.environ[_key] = ""


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Config, LLM client and registry are process singletons; reset per test."""
    from agent.config import reset_config
    from agent.llm import reset_llm_client
    from agent.mcp_client import reset_registry

    reset_config()
    reset_llm_client()
    reset_registry()
    yield
    reset_config()
    reset_llm_client()
    reset_registry()


@pytest.fixture(autouse=True)
def _assert_offline():
    """Fail loudly if a test would reach a real provider.

    A test that silently starts making live calls is slow, costs quota and
    produces results that change between runs. Better to break the suite than
    to let that happen unnoticed.
    """
    from agent.llm import get_llm_client

    assert get_llm_client().is_offline, (
        "tests must run on the offline provider; an API key leaked into the "
        "test environment"
    )
    yield
