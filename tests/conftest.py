"""Shared test configuration.

Tests run against the deterministic offline providers by default: the hash
embedder and the offline LLM. That keeps CI free, fast and reproducible, and
it means a test failure is a code change rather than a model's mood.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("EMBEDDING_BACKEND", "hash")
os.environ.setdefault("GIGBID__MODELS__RERANKER__BACKEND", "none")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-not-for-production")
os.environ.pop("OPEN_LLM_API_KEY", None)
os.environ.pop("CHECKPOINT_DATABASE_URL", None)


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
