"""Linear routes of stream S1 (connect: authorize, callback, disconnect).

FOUNDATION STUB: an empty router, included by ``api.app``; the stream adds its routes here.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/integrations/linear", tags=["linear"])
