import logging
import math

from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.metrics import CHECK_SECONDS, DECISIONS

EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/metrics"}
OUTAGE_RETRY_AFTER = "5"  # seconds a client should wait while Redis is unreachable

logger = logging.getLogger("rategate")


def identify_client(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def client_tier(request: Request) -> str:
    # Only API keys listed as paid get the paid tier; everyone else, keyless included, is free.
    api_key = request.headers.get("X-API-Key")
    return "paid" if api_key in request.app.state.paid_api_keys else "free"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        tier = client_tier(request)
        limiter = request.app.state.limiters[tier]
        try:
            with CHECK_SECONDS.time():
                decision = await limiter.check(identify_client(request))
        except RedisError as error:
            if request.app.state.fail_mode == "closed":
                DECISIONS.labels("fail_closed", tier).inc()
                logger.error(
                    "Redis unavailable, rejecting request (fail-closed): %s", error
                )
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Rate limiter unavailable. Try again shortly."},
                    headers={"Retry-After": OUTAGE_RETRY_AFTER},
                )
            DECISIONS.labels("fail_open", tier).inc()
            logger.warning("Redis unavailable, allowing request (fail-open): %s", error)
            return await call_next(request)

        DECISIONS.labels("allowed" if decision.allowed else "limited", tier).inc()

        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(math.ceil(decision.reset_after)),
            "X-RateLimit-Tier": tier,
        }

        if not decision.allowed:
            headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after)))
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Slow down."},
                headers=headers,
            )

        response = await call_next(request)
        response.headers.update(headers)
        return response
