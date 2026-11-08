from redis.asyncio import Redis

from app.config import Settings, Tier
from app.limiter.base import RateLimitStrategy
from app.limiter.sliding_counter import SlidingWindowCounter
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket


def create_limiter(
    redis: Redis, settings: Settings, tier: Tier = "free"
) -> RateLimitStrategy:
    paid = tier == "paid"
    capacity = settings.paid_capacity if paid else settings.capacity
    refill_rate = settings.paid_refill_rate if paid else settings.refill_rate
    limit = settings.paid_limit if paid else settings.limit
    window_seconds = settings.paid_window_seconds if paid else settings.window_seconds

    if settings.algorithm == "token_bucket":
        return TokenBucket(redis, capacity, refill_rate)
    if settings.algorithm == "sliding_log":
        return SlidingWindowLog(redis, limit, window_seconds)
    if settings.algorithm == "sliding_counter":
        return SlidingWindowCounter(redis, limit, window_seconds)
    raise ValueError(f"Unknown algorithm: {settings.algorithm}")
