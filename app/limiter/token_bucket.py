from redis.asyncio import Redis

from app.limiter.base import Decision, RateLimitStrategy, load_script

LUA_SCRIPT = load_script("token_bucket.lua")


class TokenBucket(RateLimitStrategy):
    def __init__(self, redis: Redis, capacity: int, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._script = redis.register_script(LUA_SCRIPT)

    async def check(self, client_id: str, cost: int = 1) -> Decision:
        key = f"rategate:tb:{client_id}"
        result = await self._script(
            keys=[key], args=[self.capacity, self.refill_rate, cost]
        )
        return Decision.from_script(result, self.capacity)
