import asyncio
import time

import httpx
import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import Settings, settings
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket
from app.main import app
from app.redis_client import create_redis

# Nothing listens on this port, so every Redis command fails like during an outage.
DOWN_URL = "redis://127.0.0.1:6399/0"


@pytest.fixture
async def redis_down():
    client = create_redis(Settings(redis_url=DOWN_URL))
    yield client
    await client.aclose()


async def call_api(limiter, fail_mode, api_key, times=1):
    """Send requests through the real app and middleware, using the given limiter."""
    app.state.limiters = {"free": limiter, "paid": limiter}
    app.state.paid_api_keys = set()
    app.state.fail_mode = fail_mode
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-API-Key": api_key}
        return [
            await client.get("/api/resource", headers=headers) for _ in range(times)
        ]


async def test_fail_open_allows_requests_when_redis_is_down(redis_down, client_id):
    [response] = await call_api(TokenBucket(redis_down, 10, 1.0), "open", client_id)
    assert response.status_code == 200
    assert "x-ratelimit-limit" not in response.headers


async def test_fail_closed_rejects_requests_when_redis_is_down(redis_down, client_id):
    [response] = await call_api(TokenBucket(redis_down, 10, 1.0), "closed", client_id)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


async def test_outage_is_detected_within_the_timeouts(redis_down, client_id):
    # A connect timeout is not retried, so the worst case is one connect timeout.
    budget = settings.redis_connect_timeout + 0.5
    start = time.perf_counter()
    await call_api(TokenBucket(redis_down, 10, 1.0), "open", client_id)
    assert time.perf_counter() - start < budget


async def test_limiting_resumes_after_an_outage(redis, redis_down, client_id):
    healthy = TokenBucket(redis, capacity=2, refill_rate=0.001)
    broken = TokenBucket(redis_down, capacity=2, refill_rate=0.001)
    before = await call_api(healthy, "closed", client_id)
    during = await call_api(broken, "closed", client_id, times=2)
    after = await call_api(healthy, "closed", client_id, times=2)
    codes = [response.status_code for response in before + during + after]
    assert codes == [200, 503, 503, 200, 429]


def test_fail_mode_accepts_only_open_or_closed():
    assert Settings(fail_mode="closed").fail_mode == "closed"
    with pytest.raises(ValidationError):
        Settings(fail_mode="maybe")


async def test_a_late_reply_is_counted_once(redis, client_id, monkeypatch):
    limiter = SlidingWindowLog(redis, limit=10, window_seconds=60)
    await limiter.check(f"{client_id}-warm")  # makes sure Redis has the script loaded
    original = Redis.parse_response

    async def late_reply(self, connection, command_name, **options):
        # The script has been sent and Redis runs it; only its answer is "late".
        # Do what redis-py does on a real read timeout: drop the connection, then raise.
        if command_name == "EVALSHA":
            await asyncio.sleep(0.05)
            await connection.disconnect(nowait=True)
            raise RedisTimeoutError("Timeout reading from Redis (simulated)")
        return await original(self, connection, command_name, **options)

    monkeypatch.setattr(Redis, "parse_response", late_reply)
    with pytest.raises(RedisError):
        await limiter.check(client_id)
    monkeypatch.undo()
    # One request, one entry. A retry after the timeout would have run it twice.
    assert await redis.zcard(f"rategate:swl:{client_id}") == 1
