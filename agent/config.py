"""Configuration loading for GigBid.

Two layers, deliberately separated:

* ``agent/config.yaml`` holds *behaviour*: model routing, thresholds, budgets,
  bidding economics, policy bindings, RAG variant choice. It is checked into
  git and is the surface the change request is absorbed through.
* ``.env`` holds *secrets and endpoints* only. Never behaviour.

Anything in the YAML can be overridden for a single process with an env var of
the form ``GIGBID__<SECTION>__<KEY>`` (double underscore separates levels), which
is what the eval harness and the ablation runner use to sweep settings without
editing the file.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "agent" / "config.yaml"

load_dotenv(REPO_ROOT / ".env", override=False)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent."""


def _coerce(raw: str) -> Any:
    """Turn an env-var string into the YAML-equivalent Python value."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _env_overrides(prefix: str = "GIGBID__") -> Iterator[tuple[list[str], Any]]:
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = [part.lower() for part in key[len(prefix) :].split("__") if part]
        if path:
            yield path, _coerce(raw)


def _set_in(tree: dict[str, Any], path: list[str], value: Any) -> None:
    node = tree
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[path[-1]] = value


class Config:
    """Read-only dotted-path view over the merged configuration tree."""

    __slots__ = ("_tree", "_path")

    def __init__(self, tree: dict[str, Any], path: Path | None = None) -> None:
        self._tree = tree
        self._path = path

    # -- access ------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._tree
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"Required configuration key missing: {dotted}")
        return value

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return dict(value) if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self._tree)

    # -- derived helpers used all over the codebase -------------------------
    @property
    def profile_name(self) -> str:
        return os.getenv("MODEL_PROFILE") or self.get("models.profile", "open")

    @property
    def marketplace_mode(self) -> str:
        return os.getenv("MARKETPLACE_MODE") or self.get("marketplace.mode", "gigsim")

    def node_model(self, node: str) -> dict[str, Any]:
        """Model settings for one graph node under the active profile."""
        profile = self.profile_name
        spec = self.get(f"models.profiles.{profile}.nodes.{node}")
        if spec is None:
            spec = self.get(f"models.profiles.open.nodes.{node}")
        if spec is None:
            raise ConfigError(
                f"No model configured for node '{node}' in profile '{profile}'. "
                "Every node must be routed explicitly in agent/config.yaml."
            )
        return dict(spec)

    def profile_endpoint(self) -> tuple[str | None, str | None]:
        """(base_url, api_key) for the active profile, read from env."""
        profile = self.profile_name
        base_env = self.get(f"models.profiles.{profile}.base_url_env")
        key_env = self.get(f"models.profiles.{profile}.api_key_env")
        return (os.getenv(base_env or ""), os.getenv(key_env or ""))

    def fallback_endpoint(self) -> tuple[str | None, str | None, str | None]:
        """(base_url, api_key, model) for the active profile's fallback, if any."""
        profile = self.profile_name
        fb = self.get(f"models.profiles.{profile}.fallback")
        if not isinstance(fb, dict):
            return (None, None, None)
        return (
            os.getenv(fb.get("base_url_env", "") or ""),
            os.getenv(fb.get("api_key_env", "") or ""),
            fb.get("model"),
        )

    def price_for(self, model: str) -> dict[str, float]:
        table = self.section("models.pricing_usd_per_mtok")
        entry = table.get(model) or {"input": 0.0, "output": 0.0}
        return {"input": float(entry.get("input", 0.0)), "output": float(entry.get("output", 0.0))}

    def variant_params(self, variant: str) -> dict[str, Any]:
        base = self.section("rag.retrieval")
        base.update(self.section(f"rag.variants.{variant}"))
        return base

    def repo_path(self, dotted: str) -> Path:
        """Resolve a config value that names a repo-relative path."""
        return REPO_ROOT / str(self.require(dotted))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Config path={self._path} profile={self.profile_name} mode={self.marketplace_mode}>"


def load_config(path: str | Path | None = None, *, apply_env: bool = True) -> Config:
    """Load ``config.yaml`` and layer ``GIGBID__*`` env overrides on top."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Configuration file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as handle:
        tree = yaml.safe_load(handle) or {}
    if not isinstance(tree, dict):
        raise ConfigError(f"{cfg_path} must contain a YAML mapping at the top level")
    if apply_env:
        for dotted_path, value in _env_overrides():
            _set_in(tree, dotted_path, value)
    return Config(tree, cfg_path)


@functools.lru_cache(maxsize=1)
def get_config() -> Config:
    """Process-wide singleton. Call ``reset_config()`` in tests that mutate env."""
    return load_config()


def reset_config() -> None:
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Secrets / endpoints. Deliberately a thin, explicit surface.
# ---------------------------------------------------------------------------
class Secrets:
    """Env-backed accessor. Values are never logged; see obs.redaction."""

    @staticmethod
    def get(name: str, default: str = "") -> str:
        return os.getenv(name, default) or default

    @staticmethod
    def require(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise ConfigError(
                f"Environment variable {name} is required but not set. "
                "Copy .env.example to .env and fill it in."
            )
        return value

    @staticmethod
    def _sqlalchemy_scheme(url: str) -> str:
        """Pin the Postgres driver SQLAlchemy should use.

        Managed hosts hand out a bare ``postgresql://`` (Render, Fly) or the
        legacy ``postgres://`` (Heroku, Supabase) URL. SQLAlchemy reads a bare
        scheme as "use psycopg2", which this project does not depend on -- only
        psycopg 3 is declared -- so the API crashes at boot with
        ``ModuleNotFoundError: psycopg2`` on a host that is configured
        perfectly correctly. Rewriting the scheme here fixes it once, rather
        than leaving every deployer to discover it from a stack trace.

        An explicit driver (``postgresql+asyncpg://``) is left alone: someone
        who named a driver meant it.
        """
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @staticmethod
    def database_url() -> str:
        url = os.getenv("DATABASE_URL")
        if not url:
            return f"sqlite:///{REPO_ROOT / 'gigbid.sqlite'}"
        return Secrets._sqlalchemy_scheme(url)

    @staticmethod
    def checkpoint_url() -> str | None:
        """Postgres URL for the LangGraph checkpointer, or None for SQLite.

        Deliberately *not* passed through ``_sqlalchemy_scheme``. The
        checkpointer hands this string to psycopg directly rather than to
        SQLAlchemy, and psycopg rejects a ``postgresql+psycopg://`` DSN. The
        two variables want different spellings of the same database.
        """
        return os.getenv("CHECKPOINT_DATABASE_URL") or None

    @staticmethod
    def redis_url() -> str:
        return os.getenv("REDIS_URL", "redis://localhost:6379/0")

    @staticmethod
    def app_secret() -> str:
        return os.getenv("APP_SECRET_KEY", "dev-insecure-secret-change-me")
