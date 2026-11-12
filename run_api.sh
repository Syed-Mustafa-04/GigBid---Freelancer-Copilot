#!/usr/bin/env bash
# Local dev launcher. `docker compose up` is the supported path; this is for
# iteration. Paths are resolved to native form because sqlite on Windows
# cannot open a Git Bash /c/... path.
set -a
[ -f .env ] && . ./.env
set +a

if command -v cygpath >/dev/null 2>&1; then
  ROOT="$(cygpath -m "$(pwd)")"
else
  ROOT="$(pwd)"
fi

export EMBEDDING_BACKEND="${EMBEDDING_BACKEND:-local}"
export REVIEWER_PASSWORD="${REVIEWER_PASSWORD:-gigbid-demo-2026}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///${ROOT}/gigbid.sqlite}"
export CHECKPOINT_SQLITE_PATH="${CHECKPOINT_SQLITE_PATH:-${ROOT}/gigbid_checkpoints.sqlite}"
unset CHECKPOINT_DATABASE_URL

exec .venv/Scripts/python.exe -m uvicorn apps.api.main:app   --port "${PORT:-8000}" --host 0.0.0.0 "$@"
