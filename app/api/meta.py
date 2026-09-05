"""Release contract exposed to browser clients and deployment probes."""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()
API_SCHEMA = "research-api.v3"
EVENT_SCHEMA = "agent_event.v1"
STARTED_AT = datetime.now(timezone.utc).isoformat()
ROOT = Path(__file__).resolve().parents[2]


def _git_sha() -> str:
    configured = os.getenv("APP_GIT_SHA") or os.getenv("GIT_SHA")
    if configured:
        return configured.strip()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=2
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _config_hash() -> str:
    path = ROOT / "app" / "config" / "harness.yml"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


@router.get("/api/meta")
async def api_meta():
    return {
        "git_sha": _git_sha(),
        "api_schema": API_SCHEMA,
        "event_schema": EVENT_SCHEMA,
        "config_hash": _config_hash(),
        "started_at": STARTED_AT,
        "environment": os.getenv("APP_ENV", "development"),
    }
