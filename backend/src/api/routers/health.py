"""``GET /healthz`` (liveness) and ``GET /readyz`` (dependencies); unauthenticated."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.deps import RuntimeDep
from api.schemas import ReadyzOut

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", response_model=ReadyzOut, responses={503: {"model": ReadyzOut}})
async def readyz(runtime: RuntimeDep) -> JSONResponse:
    report = await runtime.readiness.snapshot()
    checks: dict[str, Any] = {
        name: {
            "ok": result.ok,
            "detail": result.detail,
            "duration_ms": result.duration_ms,
        }
        for name, result in report.items()
    }
    ready = report.all_ok
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )
