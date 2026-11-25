"""Linear routes of stream S4 (check and import a Linear project or issue).

FOUNDATION STUB: an empty router, included by ``api.app``; the stream adds its routes here.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/projects/{pid}/sources/linear", tags=["sources"])
