"""Linear routes of stream S5 (gap channel target).

FOUNDATION STUB: an empty router, included by ``api.app``; the stream adds its routes here.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/projects/{pid}/linear-gaps", tags=["linear-gaps"])
