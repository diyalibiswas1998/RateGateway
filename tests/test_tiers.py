import httpx

from app.config import Settings
from app.limiter.factory import create_limiter
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket
from app.main import app


async def send(limiters, paid_api_keys, api_key, times):
    """Send requests through the real app with the given limiters and paid keys."""
    app.state.limiters = limiters
    app.state.paid_api_keys = paid_api_keys
    app.state.fail_mode = "open"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-API-Key": api_key} if api_key else {}
        return [
            await client.get("/api/resource", headers=headers) for _ in range(times)
        ]


def tiered(redis):
    # A small free limit and a bigger paid one, with nothing refilling during the test.
    return {
        "free": TokenBucket(redis, capacity=1, refill_rate=0.001),
        "paid": TokenBucket(redis, capacity=3, refill_rate=0.001),
    }


async def test_paid_keys_get_the_paid_limit(redis, client_id):
    free_key, paid_key = f"{client_id}-free", f"{client_id}-paid"
    free = await send(tiered(redis), {paid_key}, free_key, times=2)
    paid = await send(tiered(redis), {paid_key}, paid_key, times=4)
    assert [r.status_code for r in free] == [200, 429]
    assert [r.status_code for r in paid] == [200, 200, 200, 429]
    assert {r.headers["x-ratelimit-tier"] for r in free} == {"free"}
    assert {r.headers["x-ratelimit-tier"] for r in paid} == {"paid"}


async def test_clients_without_a_key_are_free(redis):
    await redis.delete("rategate:tb:ip:127.0.0.1")  # httpx's test client address
    responses = await send(tiered(redis), {"some-paid-key"}, None, times=2)
    assert [r.status_code for r in responses] == [200, 429]
    assert responses[0].headers["x-ratelimit-tier"] == "free"


def test_paid_api_keys_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RATEGATE_PAID_API_KEYS", '["key-a", "key-b"]')
    assert Settings().paid_api_keys == {"key-a", "key-b"}
    assert Settings().paid_capacity == 100


async def test_factory_builds_each_tier_with_its_own_limits(redis):
    settings = Settings(algorithm="sliding_log")
    free = create_limiter(redis, settings, "free")
    paid = create_limiter(redis, settings, "paid")
    assert isinstance(paid, SlidingWindowLog)
    assert (free.limit, paid.limit) == (settings.limit, settings.paid_limit)
