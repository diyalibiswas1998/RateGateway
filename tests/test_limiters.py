import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.limiter.factory import create_limiter
from app.limiter.sliding_counter import SlidingWindowCounter
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket

# Every algorithm allows 5 requests, plus how long to wait until it allows again.
ALGORITHMS = {
    "token_bucket": (
        lambda redis: TokenBucket(redis, capacity=5, refill_rate=1.0),
        1.1,
    ),
    "sliding_log": (
        lambda redis: SlidingWindowLog(redis, limit=5, window_seconds=1),
        1.1,
    ),
    "sliding_counter": (
        lambda redis: SlidingWindowCounter(redis, limit=5, window_seconds=1),
        2.1,
    ),
}


def make(name, redis):
    build, _ = ALGORITHMS[name]
    return build(redis)


@pytest.mark.parametrize("name", ALGORITHMS)
async def test_allows_up_to_limit_then_blocks(redis, client_id, name):
    limiter = make(name, redis)
    results = [(await limiter.check(client_id)).allowed for _ in range(7)]
    assert results == [True] * 5 + [False] * 2


@pytest.mark.parametrize("name", ALGORITHMS)
async def test_blocked_decision_says_when_to_retry(redis, client_id, name):
    limiter = make(name, redis)
    for _ in range(5):
        await limiter.check(client_id)
    decision = await limiter.check(client_id)
    assert not decision.allowed
    assert decision.remaining == 0
    assert decision.retry_after > 0


@pytest.mark.parametrize("name", ALGORITHMS)
async def test_clients_have_separate_limits(redis, client_id, name):
    limiter = make(name, redis)
    for _ in range(5):
        await limiter.check(client_id)
    assert (await limiter.check(f"{client_id}-other")).allowed


@pytest.mark.parametrize("name", ALGORITHMS)
async def test_allows_again_after_waiting(redis, client_id, name):
    build, wait_seconds = ALGORITHMS[name]
    limiter = build(redis)
    for _ in range(5):
        await limiter.check(client_id)
    assert not (await limiter.check(client_id)).allowed
    await asyncio.sleep(wait_seconds)
    assert (await limiter.check(client_id)).allowed


@pytest.mark.parametrize(
    ("name", "expected_class"),
    [
        ("token_bucket", TokenBucket),
        ("sliding_log", SlidingWindowLog),
        ("sliding_counter", SlidingWindowCounter),
    ],
)
async def test_config_picks_the_algorithm(redis, name, expected_class):
    assert isinstance(create_limiter(redis, Settings(algorithm=name)), expected_class)


def test_unknown_algorithm_is_rejected():
    with pytest.raises(ValidationError):
        Settings(algorithm="leaky_bucket")


# Boundaries: the edges of time where off-by-one and estimation bugs hide.

# How each algorithm names its key, and how long an idle key should live.
EXPIRY = {
    "token_bucket": (
        lambda redis: TokenBucket(redis, capacity=5, refill_rate=5.0),
        "rategate:tb:",
        1.0,
    ),
    "sliding_log": (
        lambda redis: SlidingWindowLog(redis, limit=5, window_seconds=1),
        "rategate:swl:",
        1.0,
    ),
    "sliding_counter": (
        lambda redis: SlidingWindowCounter(redis, limit=5, window_seconds=1),
        "rategate:swc:",
        2.0,
    ),
}


async def sleep_until(redis, offset_ms, window_ms=1000):
    """Sleep until Redis's clock is `offset_ms` into a window."""
    seconds, micros = await redis.time()
    elapsed = (seconds * 1000 + micros // 1000) % window_ms
    await asyncio.sleep(((offset_ms - elapsed) % window_ms) / 1000)


@pytest.mark.parametrize("name", ALGORITHMS)
async def test_waiting_retry_after_is_enough(redis, client_id, name):
    limiter = make(name, redis)
    for _ in range(5):
        await limiter.check(client_id)
    blocked = await limiter.check(client_id)
    assert not blocked.allowed
    await asyncio.sleep(blocked.retry_after + 0.05)
    assert (await limiter.check(client_id)).allowed


@pytest.mark.parametrize("name", EXPIRY)
async def test_key_expires_when_idle(redis, client_id, name):
    build, prefix, ttl_seconds = EXPIRY[name]
    await build(redis).check(client_id)
    key = f"{prefix}{client_id}"
    assert 0 < await redis.pttl(key) <= ttl_seconds * 1000
    await asyncio.sleep(ttl_seconds + 0.1)
    assert await redis.exists(key) == 0


@pytest.mark.parametrize("name", ["sliding_log", "sliding_counter"])
async def test_no_double_burst_across_window_boundary(redis, client_id, name):
    limiter = make(name, redis)
    await sleep_until(redis, 900)  # 100 ms before a window boundary
    before = [(await limiter.check(client_id)).allowed for _ in range(5)]
    await sleep_until(redis, 50)  # just after it
    after = [(await limiter.check(client_id)).allowed for _ in range(5)]
    assert sum(before) + sum(after) == 5
