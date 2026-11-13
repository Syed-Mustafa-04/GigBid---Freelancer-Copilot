"""GigBid API.

FastAPI, because the TypeScript client is generated from its OpenAPI schema
rather than hand-written - a hand-written client drifts, and the drift shows up
as a runtime error in the browser.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from agent.config import get_config
from agent.llm import provider_banner
from apps.api.auth.security import hash_password
from apps.api.db import SavedSearch, User, get_session_factory, init_db
from apps.api.routers.core import router


def seed_reviewer() -> None:
    """Create the reviewer login the graders use, if it does not exist."""
    email = os.getenv("REVIEWER_EMAIL", "reviewer@gigbid.app")
    password = os.getenv("REVIEWER_PASSWORD", "")
    if not password:
        return
    with get_session_factory()() as session:
        if session.scalar(select(User).where(User.email == email)):
            return
        user = User(
            email=email,
            password_hash=hash_password(password),
            display_name="Reviewer",
            persona_id=os.getenv("REVIEWER_PERSONA", "p_ai_engineer"),
            role="reviewer",
        )
        session.add(user)
        session.flush()
        session.add(
            SavedSearch(
                user_id=user.id,
                name="AI/ML engagements",
                query="retrieval augmented generation machine learning",
                skills=["retrieval augmented generation", "churn prediction"],
                interval_seconds=3600,
            )
        )
        session.commit()
        print(f"[api] seeded reviewer account: {email}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_reviewer()
    cfg = get_config()
    print(
        f"[api] GigBid up | marketplace={cfg.marketplace_mode} "
        f"profile={cfg.profile_name} rag={cfg.get('rag.shipped_variant')}"
    )
    print(f"[api] {provider_banner()}")
    yield


app = FastAPI(
    title="GigBid API",
    description=(
        "Multi-agent freelancer copilot. Finds jobs worth bidding on, drafts "
        "proposals grounded in real portfolio evidence, and never sends anything "
        "without a human approving it."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in os.getenv(
            "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",") if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "gigbid-api", "version": "1.0.0"}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a stack trace or a secret to the client."""
    from agent.safety import mask_pii

    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": mask_pii(f"{type(exc).__name__}: {exc}")[:300],
            "path": request.url.path,
        },
    )
