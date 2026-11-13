"""Encrypted OAuth token storage for live mode (A4, E4).

Tokens are encrypted with Fernet before they touch the database and are
decrypted only in the adapter that makes the call. They are never logged,
never returned by any endpoint, and never included in a trace.

If ``TOKEN_ENCRYPTION_KEY`` is unset the store refuses to save anything rather
than writing a token in plaintext. Live mode then reports that it is not
configured, which is the correct failure: a system that quietly stores
credentials unencrypted is worse than one that will not store them at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from apps.api.db import OAuthToken, get_session_factory


class TokenEncryptionUnavailable(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str = ""
    expires_at: datetime | None = None
    scopes: str = ""

    def is_expired(self, *, leeway_seconds: int = 120) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=leeway_seconds)


def _fernet():
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise TokenEncryptionUnavailable(
            "TOKEN_ENCRYPTION_KEY is not set, so OAuth tokens cannot be stored encrypted. "
            "Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


class TokenStore:
    def __init__(self, session_factory=None) -> None:
        self._sessions = session_factory or get_session_factory()

    @property
    def available(self) -> bool:
        try:
            _fernet()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def save(
        self,
        user_id: str,
        *,
        provider: str = "upwork",
        access_token: str,
        refresh_token: str = "",
        expires_in: int | None = None,
        scopes: str = "",
    ) -> None:
        fernet = _fernet()
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
        )
        with self._sessions() as session:
            row = session.scalar(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id, OAuthToken.provider == provider
                )
            )
            encrypted_access = fernet.encrypt(access_token.encode()).decode()
            encrypted_refresh = fernet.encrypt(refresh_token.encode()).decode() if refresh_token else ""
            if row is None:
                row = OAuthToken(user_id=user_id, provider=provider,
                                 access_token_enc=encrypted_access)
                session.add(row)
            row.access_token_enc = encrypted_access
            row.refresh_token_enc = encrypted_refresh
            row.expires_at = expires_at
            row.scopes = scopes
            session.commit()

    async def get(self, user_id: str, *, provider: str = "upwork") -> Token | None:
        try:
            fernet = _fernet()
        except TokenEncryptionUnavailable:
            return None
        with self._sessions() as session:
            row = session.scalar(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id, OAuthToken.provider == provider
                )
            )
            if row is None:
                return None
            return Token(
                access_token=fernet.decrypt(row.access_token_enc.encode()).decode(),
                refresh_token=(
                    fernet.decrypt(row.refresh_token_enc.encode()).decode()
                    if row.refresh_token_enc else ""
                ),
                expires_at=row.expires_at,
                scopes=row.scopes,
            )

    async def refresh(self, user_id: str, *, provider: str = "upwork") -> Token:
        """Exchange the refresh token for a new access token."""
        import httpx

        token = await self.get(user_id, provider=provider)
        if token is None or not token.refresh_token:
            raise RuntimeError(
                f"No refresh token for {provider}; the user must reconnect the account."
            )
        token_url = os.getenv("UPWORK_TOKEN_URL", "https://www.upwork.com/api/v3/oauth2/token")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                    "client_id": os.getenv("UPWORK_CLIENT_ID", ""),
                    "client_secret": os.getenv("UPWORK_CLIENT_SECRET", ""),
                },
            )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        await self.save(
            user_id,
            provider=provider,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", token.refresh_token),
            expires_in=payload.get("expires_in"),
            scopes=payload.get("scope", token.scopes),
        )
        refreshed = await self.get(user_id, provider=provider)
        assert refreshed is not None
        return refreshed

    async def delete(self, user_id: str, *, provider: str = "upwork") -> bool:
        with self._sessions() as session:
            row = session.scalar(
                select(OAuthToken).where(
                    OAuthToken.user_id == user_id, OAuthToken.provider == provider
                )
            )
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    async def status(self, user_id: str, *, provider: str = "upwork") -> dict[str, Any]:
        """Safe to return to the client: says whether a token exists, never what it is."""
        token = await self.get(user_id, provider=provider)
        return {
            "provider": provider,
            "connected": token is not None,
            "encryption_available": self.available,
            "expires_at": token.expires_at.isoformat() if token and token.expires_at else None,
            "expired": token.is_expired() if token else None,
            "scopes": token.scopes if token else "",
        }
