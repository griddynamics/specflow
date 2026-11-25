"""Linear routes of stream S3 (plan export target, run, status).

FOUNDATION STUB: an empty router, included by ``api.app``; the stream adds its routes here.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/projects/{pid}/linear-plan", tags=["linear-plan"])
