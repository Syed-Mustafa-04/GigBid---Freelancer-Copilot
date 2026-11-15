"""Database URL handling for managed hosts.

Regression cover for a boot-time crash that only appears in deployment:
Render, Fly, Heroku and Supabase all hand out a Postgres URL with a bare
scheme, SQLAlchemy reads that as "use psycopg2", and this project declares
only psycopg 3. Nothing local catches it because local development runs on
SQLite.
"""

from __future__ import annotations

import pytest

from agent.config import Secrets


class TestDatabaseUrlScheme:
    @pytest.mark.parametrize(
        "given",
        [
            "postgresql://gigbid:pw@dpg-abc123.oregon-postgres.render.com/gigbid",
            "postgres://gigbid:pw@dpg-abc123.oregon-postgres.render.com/gigbid",
        ],
    )
    def test_a_managed_host_url_is_pinned_to_psycopg3(self, monkeypatch, given):
        monkeypatch.setenv("DATABASE_URL", given)
        url = Secrets.database_url()
        assert url.startswith("postgresql+psycopg://")
        # The credentials and host must survive the rewrite untouched.
        assert url.endswith("@dpg-abc123.oregon-postgres.render.com/gigbid")
        assert "gigbid:pw" in url

    def test_an_explicit_driver_is_left_alone(self, monkeypatch):
        """Someone who named a driver meant it."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
        assert Secrets.database_url() == "postgresql+asyncpg://u:p@host/db"

    def test_sqlite_is_untouched(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///./local.sqlite")
        assert Secrets.database_url() == "sqlite:///./local.sqlite"

    def test_unset_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert Secrets.database_url().startswith("sqlite:///")

    def test_checkpoint_url_keeps_the_bare_scheme(self, monkeypatch):
        """psycopg rejects the SQLAlchemy spelling, so this one must not be rewritten."""
        monkeypatch.setenv("CHECKPOINT_DATABASE_URL", "postgresql://u:p@host/db")
        assert Secrets.checkpoint_url() == "postgresql://u:p@host/db"
