from redis.asyncio import Redis

from app.limiter.base import Decision, RateLimitStrategy, load_script

LUA_SCRIPT = load_script("sliding_log.lua")


class SlidingWindowLog(RateLimitStrategy):
    def __init__(self, redis: Redis, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_ms = int(window_seconds * 1000)
        self._script = redis.register_script(LUA_SCRIPT)

    async def check(self, client_id: str, cost: int = 1) -> Decision:
        key = f"rategate:swl:{client_id}"
        result = await self._script(
            keys=[key, f"{key}:seq"], args=[self.limit, self.window_ms, cost]
        )
        return Decision.from_script(result, self.limit)
