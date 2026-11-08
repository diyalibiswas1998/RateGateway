import httpx
from prometheus_client import REGISTRY

from app.config import Settings
from app.limiter.token_bucket import TokenBucket
from app.main import app
from app.redis_client import create_redis


def decisions(outcome, tier="free"):
    """The current value of one decision counter (0 if it hasn't been used yet)."""
    labels = {"outcome": outcome, "tier": tier}
    return REGISTRY.get_sample_value("rategate_decisions_total", labels) or 0.0


def checks():
    return REGISTRY.get_sample_value("rategate_check_seconds_count") or 0.0


async def send(limiter, api_key, times, path="/api/resource", fail_mode="open"):
    app.state.limiters = {"free": limiter, "paid": limiter}
    app.state.paid_api_keys = set()
    app.state.fail_mode = fail_mode
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-API-Key": api_key}
        return [await client.get(path, headers=headers) for _ in range(times)]


async def test_allowed_and_limited_requests_are_counted(redis, client_id):
    allowed, limited, timed = decisions("allowed"), decisions("limited"), checks()
    await send(TokenBucket(redis, capacity=1, refill_rate=0.001), client_id, times=3)
    assert decisions("allowed") - allowed == 1
    assert decisions("limited") - limited == 2
    assert checks() - timed == 3  # every check of Redis was timed


async def test_fail_open_and_fail_closed_are_counted(client_id):
    down = create_redis(Settings(redis_url="redis://127.0.0.1:6399/0"))
    try:
        limiter = TokenBucket(down, 10, 1.0)
        opened, closed = decisions("fail_open"), decisions("fail_closed")
        await send(limiter, client_id, times=1, fail_mode="open")
        await send(limiter, client_id, times=1, fail_mode="closed")
    finally:
        await down.aclose()
    assert decisions("fail_open") - opened == 1
    assert decisions("fail_closed") - closed == 1


async def test_metrics_endpoint_is_never_limited(redis, client_id):
    limiter = TokenBucket(redis, capacity=1, refill_rate=0.001)
    responses = await send(limiter, client_id, times=5, path="/metrics")
    assert [r.status_code for r in responses] == [200] * 5
    assert "rategate_decisions_total" in responses[0].text
    assert "rategate_check_seconds" in responses[0].text
