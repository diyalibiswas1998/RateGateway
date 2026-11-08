import asyncio

import pytest
from redis.asyncio import Redis

from app.config import settings
from app.limiter.sliding_counter import SlidingWindowCounter
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket
from app.redis_client import create_redis

LIMIT = 100
ATTEMPTS = 200

# Every algorithm allows exactly LIMIT, with nothing refilling during the test.
BUILDERS = {
    "token_bucket": lambda redis: TokenBucket(redis, capacity=LIMIT, refill_rate=0.001),
    "sliding_log": lambda redis: SlidingWindowLog(
        redis, limit=LIMIT, window_seconds=60
    ),
    "sliding_counter": lambda redis: SlidingWindowCounter(
        redis, limit=LIMIT, window_seconds=60
    ),
}


class NaiveCounter:
    """Deliberately broken limiter: it reads, decides and writes in separate steps."""

    def __init__(self, redis: Redis, limit: int) -> None:
        self.redis = redis
        self.limit = limit

    async def check(self, client_id: str) -> bool:
        key = f"rategate:naive:{client_id}"
        count = int(await self.redis.get(key) or 0)
        if count >= self.limit:
            return False
        await self.redis.set(key, count + 1, px=60_000)
        return True


async def warm_up(client: Redis) -> None:
    # Open the pool's connections before the burst, as a running server's pool already
    # is. The test is about atomicity, not about opening 200 connections in one instant.
    await asyncio.gather(
        *(client.ping() for _ in range(settings.redis_max_connections))
    )


@pytest.mark.parametrize("name", BUILDERS)
async def test_simultaneous_requests_never_exceed_limit(redis, client_id, name):
    limiter = BUILDERS[name](redis)
    decisions = await asyncio.gather(
        *(limiter.check(client_id) for _ in range(ATTEMPTS))
    )
    assert sum(d.allowed for d in decisions) == LIMIT


@pytest.mark.parametrize("name", BUILDERS)
async def test_limit_holds_across_separate_instances(client_id, name):
    # Four separate Redis clients and limiters, like four API servers sharing one Redis.
    clients = [create_redis(settings) for _ in range(4)]
    limiters = [BUILDERS[name](client) for client in clients]
    try:
        for client in clients:
            await warm_up(client)
        decisions = await asyncio.gather(
            *(limiters[i % 4].check(client_id) for i in range(ATTEMPTS))
        )
    finally:
        for client in clients:
            await client.aclose()
    assert sum(d.allowed for d in decisions) == LIMIT


async def test_naive_limiter_lets_too_many_through(redis, client_id):
    limiter = NaiveCounter(redis, LIMIT)
    results = await asyncio.gather(*(limiter.check(client_id) for _ in range(ATTEMPTS)))
    assert sum(results) > LIMIT
