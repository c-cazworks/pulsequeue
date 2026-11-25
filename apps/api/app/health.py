import asyncio
import logging

from fastapi import APIRouter, Request, Response, status

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Keep below the readinessProbe timeoutSeconds so we answer 503 rather than time out.
READY_TIMEOUT_SECONDS = 2.0


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process can serve requests. Never checks dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Readiness: dependencies are reachable, so this instance may receive traffic."""
    try:
        async with asyncio.timeout(READY_TIMEOUT_SECONDS):
            await request.app.state.pool.fetchval("SELECT 1")
    except Exception as exc:  # any failure means not ready; details stay in the logs
        logger.warning("readiness check failed: database: %r", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ready", "database": "ok"}
