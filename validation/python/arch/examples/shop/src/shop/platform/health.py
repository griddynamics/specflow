"""The public liveness route."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: str


@router.get("/health", response_model=HealthOut)
async def read_health() -> HealthOut:
    return HealthOut(status="ok")
