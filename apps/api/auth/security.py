"""Authentication: password hashing, JWTs, and the current-user dependency.

Multi-user because the assignment requires it and because tenancy bugs only
appear when there is more than one tenant. Every data access takes the user id
from the verified token, never from a request parameter.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from agent.config import Secrets
from apps.api.db import User, get_db

ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "720"))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256. Chosen over bcrypt to avoid the 72-byte truncation
    surprise and the passlib/bcrypt version incompatibilities, while staying
    in the standard library."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390_000)
    return f"pbkdf2_sha256$390000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def create_access_token(user_id: str, *, email: str, role: str, minutes: int | None = None) -> str:
    expires = datetime.now(UTC) + timedelta(minutes=minutes or ACCESS_TOKEN_MINUTES)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": expires,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, Secrets.app_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, Secrets.app_secret(), algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
async def current_user(
    token: str | None = Depends(oauth2_scheme), session: Session = Depends(get_db)
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(token)
    user = session.get(User, payload.get("sub"))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def optional_user(
    token: str | None = Depends(oauth2_scheme), session: Session = Depends(get_db)
) -> User | None:
    if not token:
        return None
    try:
        payload = decode_token(token)
    except HTTPException:
        return None
    return session.get(User, payload.get("sub"))
