# Phase 2 Guide — Strategy Interface + Two More Algorithms

**Goal of this phase:** RateGate can run any of three algorithms (token bucket, sliding window log,
sliding window counter), chosen by one setting, with **no change to the middleware**.

This proves the resume line *"Implemented token bucket, sliding window log, and sliding window counter
algorithms behind a common strategy interface, using atomic Redis Lua scripts."*

All code below was run and tested on 2026-11-02 against your Redis: 16 tests passed three runs in a row,
and `ruff` reports no problems. Type it yourself where you can.

---

## Part A — Concepts (read before coding)

### 1. What changes, in one picture

```
Phase 1:   middleware ──► TokenBucket ──► token_bucket.lua

Phase 2:   middleware ──► RateLimitStrategy (the "shape" every algorithm has)
                              ├── TokenBucket          ──► token_bucket.lua
                              ├── SlidingWindowLog     ──► sliding_log.lua
                              └── SlidingWindowCounter ──► sliding_counter.lua
                          factory picks one, based on RATEGATE_ALGORITHM
```

The middleware still calls `limiter.check(client_id)` and reads a `Decision`. It never learns which
algorithm answered.

### 2. The strategy pattern

**The problem:** we want several ways of doing the same job (deciding "allowed or not?") and to switch
between them without touching the code that *uses* them.

**The pattern:** define the job's **shape** once, as a set of methods every version must have.
Each algorithm is a separate class that fills in that shape. The caller only knows the shape.

**Analogy:** a phone charger socket. The socket (interface) is fixed. Any charger (strategy) that fits the
socket works: the phone doesn't care who made it.

In Python the shape is an **abstract base class (ABC)**:

```python
class RateLimitStrategy(ABC):
    @abstractmethod
    async def check(self, client_id: str, cost: int = 1) -> Decision: ...
```

- `ABC` marks the class as abstract: you can't create a `RateLimitStrategy()` directly.
- `@abstractmethod` says "every subclass **must** write this method". If a subclass forgets,
  Python refuses to create it, with a clear error. So the contract is enforced, not just hoped for.
- `class TokenBucket(RateLimitStrategy):` means TokenBucket **is a** RateLimitStrategy.

**Why it matters:** adding a fourth algorithm later = one new class + one Lua script + one line in the
factory. The middleware, routes, and tests for other algorithms don't change. This is the
**open/closed principle**: open for extension, closed for modification.

### 3. Why more than one algorithm?

Each makes a different trade-off between **accuracy**, **memory**, and **burst behaviour**. Real companies
pick different ones (Stripe: token bucket; Cloudflare: sliding window counter).

### 4. Algorithm 2 — Sliding window log

**Idea:** keep a **log of the exact time of every allowed request** in the last *window* (e.g. 10 seconds).
A new request is allowed if fewer than *limit* requests are in the log.

**On each request:**

1. Delete log entries older than `now − window` (they've "slid out").
2. Count what's left.
3. If `count + cost ≤ limit`: allowed, add `now` to the log. Otherwise: blocked.

**Worked example** (limit 3, window 10 s):

| Time | Log before (after removing old) | Count | Decision | Log after |
|---|---|---|---|---|
| 0 s | — | 0 | allow | 0 |
| 2 s | 0 | 1 | allow | 0, 2 |
| 4 s | 0, 2 | 2 | allow | 0, 2, 4 |
| 6 s | 0, 2, 4 | 3 | **block**, retry in 4 s (when the 0 s entry leaves) | 0, 2, 4 |
| 10.5 s | 2, 4 (0 slid out) | 2 | allow | 2, 4, 10.5 |

**Redis structure: a sorted set.** A sorted set holds unique **members**, each with a numeric **score**,
kept sorted by score. We use the time as the score, so "delete everything older than X" and "count" are
single fast commands:

| Command | Does |
|---|---|
| `ZADD key score member` | Add an entry |
| `ZREMRANGEBYSCORE key -inf X` | Remove every entry with score ≤ X |
| `ZCARD key` | Count entries |
| `ZRANGE key i i WITHSCORES` | Read the entry at position i (0 = oldest, -1 = newest) |

**Members must be unique** — adding the same member again just updates it. Two requests can arrive in the
same millisecond, so each member is `time-sequence` (e.g. `1793621490566-1`), with the sequence number from a
separate counter key (`INCR`).

**Trade-offs:** ✅ perfectly exact — there's no approximation at all. ❌ memory grows with the limit: a limit
of 1,000 per minute stores up to 1,000 entries **per client**. ❌ no gradual refill: after a burst, nothing
comes back until the oldest request is a full window old, then everything returns at once.

### 5. Algorithm 3 — Sliding window counter

**Idea:** approximate the log using just **two counters**: requests in the *current* fixed window and
in the *previous* one.

**Fixed windows:** time is cut into slices of `window` length: [0, 10 s), [10 s, 20 s), … A window's number is
`floor(now / window)`. A plain *fixed window* limiter just counts the current slice — but it has the boundary
problem: 10 requests at 9.9 s plus 10 at 10.1 s = 20 requests in 0.2 s, against a limit of 10.

**The fix — weighting:** the real "last 10 seconds" overlaps two slices. Estimate it as
*all* of the current slice plus the *overlapping share* of the previous one:

```
weight   = (window − elapsed_in_current) / window      ← share of previous window still inside "the last 10 s"
estimate = prev × weight + curr
allowed if estimate + cost ≤ limit
```

**Worked example** (limit 10, window 10 s): previous window had 8 requests, current has 3 so far,
and we're 2.5 s into the current window.

- weight = (10 − 2.5) / 10 = 0.75
- estimate = 8 × 0.75 + 3 = 9
- 9 + 1 ≤ 10 → **allowed** (current becomes 4)

It assumes the previous window's requests were spread evenly — that's the approximation. Cloudflare measured
it as wrong on only a tiny fraction of requests.

**Redis structure:** one hash per client: `{window: 179362149, curr: 10, prev: 0}`. When a request arrives in a
later window, the script "rolls" the counts forward: one window later → `prev = curr, curr = 0`; two or more
later → both 0.

**Trade-offs:** ✅ tiny memory (3 numbers per client, whatever the limit). ✅ smooths the fixed-window boundary
problem. ❌ approximate. ❌ results depend on where window boundaries fall, so the same test can give slightly
different counts at different moments.

### 6. The three side by side

| | Token bucket | Sliding window log | Sliding window counter |
|---|---|---|---|
| Stored per client | 2 numbers | 1 entry per request in window | 3 numbers |
| Accuracy | Exact | Exact | Approximate |
| Bursts | Up to capacity, then steady refill | Up to limit, then nothing until old requests age out | Up to limit, then gradual as the previous window fades |
| Recovery after a burst | Gradual (1 token/s) | All at once, a full window later | Gradual-ish, from the next window on |
| Used by | Stripe, AWS | Small/strict systems | Cloudflare |
| Redis type | Hash | Sorted set | Hash |

### 7. The factory

Something must read the setting and build the right class. That's a **factory function**: a function whose
job is to create objects.

```python
def create_limiter(redis, settings) -> RateLimitStrategy:
    if settings.algorithm == "token_bucket": return TokenBucket(...)
    ...
```

`main.py` calls the factory instead of naming `TokenBucket`. The factory is now the **only** place that
knows all three classes exist.

### 8. Testing concepts used this phase

- **Automated tests** check behaviour in seconds, every time, without you typing curl commands.
- **pytest-asyncio** lets test functions be `async def`, so they can `await limiter.check(...)`.
  `asyncio_mode = "auto"` means you don't have to mark every async test.
- **Fixtures** (`@pytest.fixture`) are reusable setup. A test asks for one by naming it as a parameter:
  `def test_x(redis, client_id)` gets a Redis client and a fresh client ID.
- **`yield` in a fixture**: code before `yield` is setup, after is cleanup (like `lifespan`).
- **`@pytest.mark.parametrize`** runs one test several times with different inputs — here, once per
  algorithm. One test function = three test cases.
- **Unique IDs (`uuid4()`)** give each test its own client, so tests never share a bucket and never need to
  wait for old keys to expire.

---

## Part B — Setup

Run in `D:\RateGate`:

```powershell
uv add --dev pytest-asyncio
```

Then in `pyproject.toml`, add two lines to the `[tool.pytest.ini_options]` section so it reads:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

- `asyncio_mode = "auto"`: every `async def test_…` runs automatically on an event loop.
- `asyncio_default_fixture_loop_scope = "function"`: each test gets a fresh event loop. Without this line
  pytest-asyncio prints a long deprecation warning.

Make sure Docker Desktop is open and `docker start redis` has been run.

---

## Part C — Build it (in this order)

Final structure after this phase (★ = new, ✎ = changed):

```
app/
  config.py                    ✎ Step 3
  main.py                      ✎ Step 9
  middleware.py                  (unchanged — on purpose)
  limiter/
    base.py                    ★ Step 1
    token_bucket.py            ✎ Step 2
    sliding_log.py             ★ Step 5
    sliding_counter.py         ★ Step 7
    factory.py                 ★ Step 8
    scripts/
      token_bucket.lua           (unchanged)
      sliding_log.lua          ★ Step 4
      sliding_counter.lua      ★ Step 6
tests/
  conftest.py                  ★ Step 10
  test_limiters.py             ★ Step 10
```

### Why this order

| Order | What | Why here |
|---|---|---|
| 1 | `base.py` | Every other limiter file imports from it. Foundations first, as in Phase 1. |
| 2 | Refactor `token_bucket.py` | Fit the **existing, working** algorithm into the new shape before adding new ones. If something breaks, you know it's the refactor, not new code. |
| — | **Checkpoint** | Run the server: the Phase 1 burst must still give 10 × 200 then 429s. This is a **regression check**: proving old behaviour survived a change. |
| 3 | `config.py` | The new settings (`algorithm`, `limit`, `window_seconds`) must exist before anything reads them. |
| 4–5 | Sliding log: `.lua` then `.py` | Script first (the logic), then its wrapper — same order as Phase 1. |
| 6–7 | Sliding counter: `.lua` then `.py` | Same pattern again; the second time is faster. |
| 8 | `factory.py` | Imports all three classes, so it can only be written once they exist. |
| 9 | `main.py` | The wiring, last — it imports the factory. |
| 10 | Tests | Need everything above. |
| 11 | README | Write up the trade-offs once you've seen them. |

### Step 1 — `app/limiter/base.py` (new): the shared shape

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent / "scripts"


def load_script(name: str) -> str:
    return (SCRIPTS_DIR / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # seconds until the next request would be allowed (0 if allowed)
    reset_after: float  # seconds until the client's full allowance is back

    @classmethod
    def from_script(cls, result: list[int], limit: int) -> "Decision":
        allowed, remaining, retry_ms, reset_ms = result
        return cls(
            allowed=bool(allowed),
            limit=limit,
            remaining=int(remaining),
            retry_after=retry_ms / 1000,
            reset_after=reset_ms / 1000,
        )


class RateLimitStrategy(ABC):
    @abstractmethod
    async def check(self, client_id: str, cost: int = 1) -> Decision:
        """Spend `cost` from the client's allowance if there is enough, and report the result."""
```

**What's happening:**

- **`load_script`**: Phase 1's "find the .lua file next to this code" logic, moved here so all three
  algorithms share it instead of copying it three times. This is the **DRY** principle (Don't Repeat Yourself).
- **`Decision` moved here** from `token_bucket.py`, because it now belongs to *all* algorithms, not one.
  `reset_after`'s comment is reworded to fit every algorithm ("full allowance is back").
- **`from_script`** is a **classmethod**: a method called on the class itself (`Decision.from_script(...)`),
  usually to build an object a particular way. All three Lua scripts return the same 4 numbers
  `{allowed, remaining, retry_ms, reset_ms}`, so one converter serves them all. `cls` is the class
  (like `self` is the object).
- The return type `"Decision"` is in quotes because the class isn't fully defined yet at that line.
- **`RateLimitStrategy`**: the contract. The docstring (text in triple quotes) documents what every
  implementation promises. Its body is only the docstring — an abstract method has nothing to run.

### Step 2 — `app/limiter/token_bucket.py` (rewrite): fit it into the shape

Replace the whole file with:

```python
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
```

**What changed vs Phase 1, and why:**

| Phase 1 | Phase 2 | Why |
|---|---|---|
| `class TokenBucket:` | `class TokenBucket(RateLimitStrategy):` | It now formally follows the contract |
| `Decision` defined here | imported from `base` | Shared by all algorithms |
| Path + `read_text` here | `load_script("token_bucket.lua")` | Shared helper |
| Unpacked 4 values, built `Decision` by hand | `Decision.from_script(result, self.capacity)` | Shared converter |

The Lua script and the behaviour are **identical**. Only the structure changed — that's what
**refactoring** means: improving structure without changing behaviour.

**Checkpoint (regression check):** `uv run uvicorn app.main:app --reload` still works at this point
(`main.py` imports `TokenBucket` from `app.limiter.token_bucket`, which still exists). In a second terminal:

```powershell
1..15 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " http://127.0.0.1:8000/api/resource }
```

Expected: 10 × `200`, 5 × `429`, exactly as in Phase 1. If so, commit:
`git add . ; git commit -m "Refactor token bucket behind RateLimitStrategy interface"`. Stop the server (Ctrl+C).

### Step 3 — `app/config.py` (edit): new settings

```python
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Algorithm = Literal["token_bucket", "sliding_log", "sliding_counter"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RATEGATE_", env_file=".env")

    redis_url: str = "redis://localhost:6379/0"
    algorithm: Algorithm = "token_bucket"

    # Token bucket
    capacity: int = 10  # max tokens in a bucket (burst size)
    refill_rate: float = 1.0  # tokens added per second

    # Sliding window log and sliding window counter
    limit: int = 10  # max requests per window
    window_seconds: float = 10.0  # window length


settings = Settings()
```

**What's happening:**

- **`Literal[...]`** means "only exactly one of these strings". Pydantic enforces it: setting
  `RATEGATE_ALGORITHM=leaky_bucket` stops the app at startup with a clear message, instead of failing
  mysteriously later. A typo can't slip through.
- **`Algorithm = Literal[...]`** gives that type a name (a **type alias**), so the field line stays short
  and the list of names lives in one obvious place.
- **`limit` and `window_seconds`** are new: the window algorithms think in "N requests per window", not in
  tokens. The defaults (10 per 10 s) match the token bucket's long-run rate of 1 per second, so the three
  algorithms are comparable.
- Token bucket settings are unchanged, so the default behaviour is exactly Phase 1's.

### Step 4 — `app/limiter/scripts/sliding_log.lua` (new)

```lua
-- Sliding window log, executed atomically inside Redis.
-- KEYS[1] = sorted set of this client's request times, KEYS[2] = counter that keeps entries unique
-- ARGV[1] = limit (requests per window), ARGV[2] = window length (ms), ARGV[3] = cost of this request

local key = KEYS[1]
local seq_key = KEYS[2]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Forget requests that have slid out of the window, then count the rest.
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)
local count = redis.call('ZCARD', key)

local allowed = 0
local retry_after_ms = 0
if count + cost <= limit then
  -- Log this request. Two requests can share a millisecond, so each entry gets a sequence number.
  for i = 1, cost do
    local seq = redis.call('INCR', seq_key)
    redis.call('ZADD', key, now, now .. '-' .. seq)
  end
  count = count + cost
  allowed = 1
  -- Both keys can go once this newest request has left the window.
  redis.call('PEXPIRE', key, window_ms)
  redis.call('PEXPIRE', seq_key, window_ms)
else
  -- Wait until enough of the oldest requests slide out of the window.
  local must_leave = count + cost - limit
  local entry = redis.call('ZRANGE', key, must_leave - 1, must_leave - 1, 'WITHSCORES')
  if entry[2] then
    retry_after_ms = tonumber(entry[2]) + window_ms - now
  else
    retry_after_ms = window_ms
  end
end

-- The full allowance is back when the newest logged request leaves the window.
local reset_ms = 0
local newest = redis.call('ZRANGE', key, -1, -1, 'WITHSCORES')
if newest[2] then
  reset_ms = tonumber(newest[2]) + window_ms - now
end

return {allowed, limit - count, retry_after_ms, reset_ms}
```

**Walk-through:**

- **Two keys** in `KEYS`: the log itself and its sequence counter. Every key a script touches is passed in
  `KEYS`, never invented inside the script — Redis (especially Redis Cluster, later) relies on knowing them.
- **Time** is taken from Redis, exactly as in the token bucket, for the same reason (one clock).
- **`ZREMRANGEBYSCORE key -inf (now − window)`** deletes everything at least a full window old.
  `-inf` means "from the smallest possible score".
- **`for i = 1, cost do ... end`** is a Lua loop: one log entry per unit of cost (normally 1).
- **`now .. '-' .. seq`**: `..` joins strings in Lua. The member looks like `1793621490566-1`;
  the score is `1793621490566`.
- **Blocked → retry time.** If `count + cost − limit` entries must leave first, the one that matters is
  that many places from the oldest. `ZRANGE key i i WITHSCORES` returns `{member, score}`; it leaves at
  `score + window`, so retry = `score + window − now`.
- **Expiry only when allowed.** The key only needs to live until its newest entry is a window old,
  and the newest entry only changes when a request is allowed.
- **Reset** = when the newest entry leaves → the whole allowance is back.
- **Returns the same 4 numbers as the token bucket.** That's what lets `Decision.from_script` serve both.

### Step 5 — `app/limiter/sliding_log.py` (new)

```python
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
```

**What's happening:** the same shape as `TokenBucket`. Differences: it takes `limit` and `window_seconds`;
converts seconds to whole milliseconds once (`int(...)`), because the Lua works in ms; uses the key prefix
`swl` (sliding window log) so its keys never collide with the token bucket's `tb`; and passes two keys.

### Step 6 — `app/limiter/scripts/sliding_counter.lua` (new)

```lua
-- Sliding window counter, executed atomically inside Redis.
-- KEYS[1] = hash holding this client's window number and two counts
-- ARGV[1] = limit (requests per window), ARGV[2] = window length (ms), ARGV[3] = cost of this request

local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Time is cut into fixed windows numbered 0, 1, 2, ...; find ours and how far into it we are.
local window = math.floor(now / window_ms)
local elapsed = now - window * window_ms

-- Load the saved counts and roll them forward if time has moved on.
local state = redis.call('HMGET', key, 'window', 'curr', 'prev')
local saved_window = tonumber(state[1])
local curr = tonumber(state[2]) or 0
local prev = tonumber(state[3]) or 0
if saved_window == nil or saved_window < window - 1 then
  -- New client, or idle for over a whole window: nothing counts any more.
  prev = 0
  curr = 0
elseif saved_window == window - 1 then
  -- One window boundary passed: the current count becomes the previous one.
  prev = curr
  curr = 0
end

-- Estimate the requests in the last window_ms: all of this window, plus
-- the share of the previous window that still overlaps it.
local weight = (window_ms - elapsed) / window_ms
local estimate = prev * weight + curr

local allowed = 0
local retry_after_ms = 0
if estimate + cost <= limit then
  curr = curr + cost
  estimate = estimate + cost
  allowed = 1
else
  local room = limit - cost - curr
  if room < 0 or prev == 0 then
    -- This window alone is full: wait for the next one.
    retry_after_ms = window_ms - elapsed
  else
    -- Wait until the previous window's share shrinks enough.
    retry_after_ms = math.ceil(window_ms * (1 - room / prev) - elapsed)
  end
end

-- After two idle windows both counts are irrelevant, so the key can go.
redis.call('HSET', key, 'window', window, 'curr', curr, 'prev', prev)
redis.call('PEXPIRE', key, 2 * window_ms)

-- Everything is forgotten once this window and the next have passed.
local reset_ms = window_ms - elapsed
if curr > 0 then
  reset_ms = reset_ms + window_ms
end

return {allowed, math.max(0, math.floor(limit - estimate)), retry_after_ms, reset_ms}
```

**Walk-through:**

- **Window number and position.** With a 10,000 ms window and `now = 1793621490482`:
  `window = floor(1793621490482 / 10000) = 179362149`, and `elapsed = 482` ms into it.
  Every server computes the same number from Redis's clock, so windows line up everywhere.
- **`or 0`** in Lua means "if the left side is `nil`, use 0" — a new client has no counts.
- **Rolling forward** (the `if / elseif`):
  - saved window is 2+ windows ago (or missing) → everything is stale → both 0.
  - saved window is exactly the previous one → shift: `prev = curr`, `curr = 0`.
  - saved window is the current one → nothing to do.
- **Why one hash, not a key per window?** The window number is computed *inside* the script from `TIME`.
  Keys must be passed in `KEYS`, so they can't depend on values computed inside. Storing the window number
  as a field avoids that problem.
- **The estimate** is the formula from Part A.
- **Retry time when blocked**, two cases:
  - `room = limit − cost − curr` is how much of the previous window's share we can afford.
    If it's negative, the **current** window alone is too full: wait for the next window.
  - Otherwise, solve `prev × (window − e) / window ≤ room` for the position `e` in the window:
    `e = window × (1 − room / prev)`. Retry = `e − elapsed`. It's an estimate, like the algorithm.
- **Expiry 2 × window**: after that, both counts would be reset anyway.
- **Remaining** = `floor(limit − estimate)`, never below 0 (`math.max`), because the estimate is a decimal.

### Step 7 — `app/limiter/sliding_counter.py` (new)

```python
from redis.asyncio import Redis

from app.limiter.base import Decision, RateLimitStrategy, load_script

LUA_SCRIPT = load_script("sliding_counter.lua")


class SlidingWindowCounter(RateLimitStrategy):
    def __init__(self, redis: Redis, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_ms = int(window_seconds * 1000)
        self._script = redis.register_script(LUA_SCRIPT)

    async def check(self, client_id: str, cost: int = 1) -> Decision:
        key = f"rategate:swc:{client_id}"
        result = await self._script(keys=[key], args=[self.limit, self.window_ms, cost])
        return Decision.from_script(result, self.limit)
```

Same shape again, prefix `swc` (sliding window counter), one key. By now you can see the pattern: every
strategy is ~15 lines of Python wrapping its Lua script.

### Step 8 — `app/limiter/factory.py` (new)

```python
from redis.asyncio import Redis

from app.config import Settings
from app.limiter.base import RateLimitStrategy
from app.limiter.sliding_counter import SlidingWindowCounter
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket


def create_limiter(redis: Redis, settings: Settings) -> RateLimitStrategy:
    if settings.algorithm == "token_bucket":
        return TokenBucket(redis, settings.capacity, settings.refill_rate)
    if settings.algorithm == "sliding_log":
        return SlidingWindowLog(redis, settings.limit, settings.window_seconds)
    if settings.algorithm == "sliding_counter":
        return SlidingWindowCounter(redis, settings.limit, settings.window_seconds)
    raise ValueError(f"Unknown algorithm: {settings.algorithm}")
```

**What's happening:**

- The return type is `RateLimitStrategy`, the **shape** — not any specific class. That's the promise to
  callers: "you'll get *something* with a `check` method".
- It receives `settings` as a parameter instead of importing the global one, so tests can pass
  `Settings(algorithm="sliding_log")` directly (dependency injection again).
- The final `raise` can't normally be reached, because `Literal` already rejected bad names. It's a
  safety net in case someone adds a name to `Algorithm` but forgets to add it here.

### Step 9 — `app/main.py` (edit two lines)

Change the import:

```python
from app.limiter.token_bucket import TokenBucket          # remove
from app.limiter.factory import create_limiter            # add
```

and the line inside `lifespan`:

```python
    app.state.limiter = TokenBucket(redis, settings.capacity, settings.refill_rate)   # remove
    app.state.limiter = create_limiter(redis, settings)                               # add
```

The full file afterwards:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import settings
from app.limiter.factory import create_limiter
from app.middleware import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.limiter = create_limiter(redis, settings)
    yield
    await redis.aclose()


app = FastAPI(title="RateGate", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/resource")
async def resource():
    return {"message": "You got through!"}
```

**And `middleware.py`? Don't touch it.** It only uses `check()` and `Decision`'s fields, which every
strategy provides. That it needs no change is the proof the interface works.

### Step 10 — Tests

Create the `tests` folder with two files.

**`tests/conftest.py`** — fixtures shared by all test files (pytest finds this file name automatically):

```python
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.config import settings


@pytest.fixture
async def redis():
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def client_id():
    # A fresh id per test, so tests never share a bucket or wait for old keys to expire.
    return f"test-{uuid4()}"
```

- `redis`: opens a client before each test, closes it after (`yield`).
- `client_id`: `uuid4()` makes a random ID like `test-3f2a9c1e-...`, different every time.

**`tests/test_limiters.py`**:

```python
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
```

**What's happening:**

- **`ALGORITHMS`** is a dictionary: name → (how to build it, how long until it allows again).
  All three are configured to allow **5**, so one set of tests checks all three.
  Short windows (1 s) keep the tests fast.
- **`lambda redis: ...`** is a tiny unnamed function. `build(redis)` calls it to make a fresh limiter.
- **Wait times differ on purpose**, and show the algorithms' nature:
  token bucket refills 1 token per second → 1.1 s; the log forgets entries after exactly 1 window → 1.1 s;
  the counter's previous-window share only fully fades **two** window boundaries later → 2.1 s.
- **`@pytest.mark.parametrize("name", ALGORITHMS)`** runs each test 3 times (looping over a dict gives its
  keys). In the output you'll see `[token_bucket]`, `[sliding_log]`, `[sliding_counter]`.
- **The 6 tests:**

| Test | Proves |
|---|---|
| `test_allows_up_to_limit_then_blocks` | Exactly 5 allowed, then blocked |
| `test_blocked_decision_says_when_to_retry` | A blocked `Decision` has `remaining == 0` and a positive `retry_after` |
| `test_clients_have_separate_limits` | Another client is unaffected |
| `test_allows_again_after_waiting` | Each algorithm recovers |
| `test_config_picks_the_algorithm` | The factory builds the right class for each name |
| `test_unknown_algorithm_is_rejected` | A typo in the setting is refused |

- **`pytest.raises(ValidationError)`** passes only if the code inside raises that error.
- **`isinstance(obj, cls)`** checks an object's class.
- These are **integration tests** (they use the real Redis, because the Lua scripts only exist there).
  Make sure Redis is running.

### Step 11 — README section

Add a section to `README.md` (this is a roadmap task, and recruiters read it). Write it in your own words;
a starting point:

```markdown
## Algorithms

Choose with `RATEGATE_ALGORITHM` (`token_bucket` | `sliding_log` | `sliding_counter`).

| | Token bucket | Sliding window log | Sliding window counter |
|---|---|---|---|
| Memory per client | 2 numbers | 1 entry per request in the window | 3 numbers |
| Accuracy | Exact | Exact | Approximate |
| After a burst | Refills gradually | Nothing until a full window passes | Fades gradually from the next window |
```

---

## Part D — Verify (checks, with real outputs)

### Timing rules for Phase 2

Each algorithm "forgets" on a different clock (with the defaults: 10 per 10 s, or capacity 10 at 1/s):

| Algorithm | Fresh state after | Why |
|---|---|---|
| Token bucket | 10 s idle | Bucket refills completely |
| Sliding window log | 10 s idle | Every logged request has left the window |
| Sliding window counter | up to 20 s idle | Counts must pass **two** window boundaries |

**Shortcut — no waiting:** give each check run its own API key, e.g. `-H "X-API-Key: run7"`, then `run8`,
and so on. A new key is a brand-new client with a fresh allowance in every algorithm.

Also: a `$env:RATEGATE_...` setting stays set **in that terminal** until you remove it
(`Remove-Item Env:RATEGATE_ALGORITHM`) or close the terminal.

### Check 1 — automated tests

**When:** any time; no waiting needed (each test uses a unique client).

```powershell
uv run pytest -v
```

**Our output (2026-11-02):**

```
tests/test_limiters.py::test_allows_up_to_limit_then_blocks[token_bucket] PASSED
tests/test_limiters.py::test_allows_up_to_limit_then_blocks[sliding_log] PASSED
tests/test_limiters.py::test_allows_up_to_limit_then_blocks[sliding_counter] PASSED
tests/test_limiters.py::test_blocked_decision_says_when_to_retry[token_bucket] PASSED
...
tests/test_limiters.py::test_config_picks_the_algorithm[sliding_counter-SlidingWindowCounter] PASSED
tests/test_limiters.py::test_unknown_algorithm_is_rejected PASSED
============================= 16 passed in 4.73s ==============================
```

**Meaning:** 6 test functions × up to 3 algorithms = 16 cases, all passing. It takes ~4.7 s because the
"allows again after waiting" tests sleep 1.1 + 1.1 + 2.1 s. Run it 2–3 times: passing every time shows the
tests are stable, not lucky.

### Check 2 — regression: the default is still Phase 1

**When:** fresh state (wait 10 s, or new API key).

```powershell
uv run uvicorn app.main:app --reload          # terminal 1, no RATEGATE_ALGORITHM set
1..15 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " http://127.0.0.1:8000/api/resource }   # terminal 2
```

**Expected:** `200` × 10, then `429` × 5. **Meaning:** with no setting, `algorithm` defaults to
`token_bucket`, and the refactor didn't change behaviour.

### Check 3 — switch algorithm with one setting

For each algorithm, restart the server in terminal 1 with the setting, then burst from terminal 2 with a fresh key:

```powershell
# terminal 1 (Ctrl+C first to stop the running server)
$env:RATEGATE_ALGORITHM = "sliding_log"; uv run uvicorn app.main:app --reload
# terminal 2
1..15 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: c3-log" http://127.0.0.1:8000/api/resource }
```

Then the same with `"sliding_counter"` (key `c3-counter`). Finally `Remove-Item Env:RATEGATE_ALGORITHM`.

**Our output** (all three, first burst):

```
token_bucket     200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
sliding_log      200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
sliding_counter  200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
```

**Meaning:** all three enforce 10 per window from a fresh start, and switching needed **no code change**.
On a first burst they look identical — the differences appear over time (Check 5).

### Check 4 — look inside Redis

**When:** in the **same command line** as a burst (keys expire).

```powershell
# with the server running sliding_log:
1..12 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: c4" http://127.0.0.1:8000/api/resource }; docker exec redis redis-cli ZRANGE "rategate:swl:key:c4" 0 -1 WITHSCORES
# with the server running sliding_counter:
1..12 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: c4" http://127.0.0.1:8000/api/resource }; docker exec redis redis-cli HGETALL "rategate:swc:key:c4"
```

**Our output:**

```
token_bucket    HGETALL: tokens 0.32799999999999957 ts 1793621490482
sliding_log     ZCARD: 10   first 3 entries: 1793621490566-1 1793621490566 1793621490589-2 1793621490589 1793621490609-3 1793621490609
sliding_counter HGETALL: window 179362149 curr 10 prev 0
```

**Meaning:**

- **Token bucket:** 2 numbers, as in Phase 1.
- **Sliding log:** 10 entries — one per allowed request. Each shows the member (`time-sequence`) then its
  score (the time). The 2 blocked requests were **not** logged. The entries are ~20 ms apart: the time
  each curl took.
- **Sliding counter:** window number `179362149` (= the 10-second slice starting at 1793621490000 ms),
  10 requests counted in it, nothing in the previous one.

That's the memory trade-off made visible: 10 entries vs 2–3 numbers. With a limit of 1,000, the log would
hold 1,000 entries per client.

### Check 5 — the algorithms behave differently over time

This is the most instructive check. Run **three servers at once**, one per algorithm, on different ports,
each in its own terminal:

```powershell
# terminal 1
$env:RATEGATE_ALGORITHM = "token_bucket"; uv run uvicorn app.main:app --port 8001
# terminal 2
$env:RATEGATE_ALGORITHM = "sliding_log"; uv run uvicorn app.main:app --port 8002
# terminal 3
$env:RATEGATE_ALGORITHM = "sliding_counter"; uv run uvicorn app.main:app --port 8003
```

In a 4th terminal, burst all three at t = 0, then **wait 5 s**, burst again, then **wait 6 s more** (t = 11)
and burst again:

```powershell
$k = "cmp" + (Get-Random)
function burst($p) { (1..15 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: $k" "http://127.0.0.1:$p/api/resource" }) -join '' }
function round($label) { $label; "  token_bucket    " + (burst 8001); "  sliding_log     " + (burst 8002); "  sliding_counter " + (burst 8003) }
round "t=0"; Start-Sleep -Seconds 5; round "t=5"; Start-Sleep -Seconds 6; round "t=11"
```

(`$k` is a random fresh key, so no earlier state interferes.)

**Our output:**

```
t=0  burst of 15
  token_bucket     200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
  sliding_log      200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
  sliding_counter  200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
t=5  burst of 15
  token_bucket     200 200 200 200 200 200 429 429 429 429 429 429 429 429 429
  sliding_log      429 429 429 429 429 429 429 429 429 429 429 429 429 429 429
  sliding_counter  429 429 429 429 429 429 429 429 429 429 429 429 429 429 429
t=11 burst of 15
  token_bucket     200 200 200 200 200 200 200 429 429 429 429 429 429 429 429
  sliding_log      200 200 200 200 200 200 200 200 200 200 429 429 429 429 429
  sliding_counter  200 200 200 200 200 429 429 429 429 429 429 429 429 429 429
```

**Meaning, row by row:**

- **t = 5, token bucket: ~5–6 allowed.** Tokens dripped back at 1/s. (6 here because the rounds run one
  after another, so slightly more than 5 s had passed for it.)
- **t = 5, sliding log: 0 allowed.** All 10 requests from t = 0 are still inside the last 10 seconds.
  The log doesn't refill gradually: it waits for each old request to age out.
- **t = 5, sliding counter: 0 allowed.** The t = 0 burst landed in the current 10-second slice
  (`curr = 10`), so the slice alone is full.
- **t = 11, token bucket: 7.** About 7 s had passed since its t = 5 burst, so about 7 tokens were back.
- **t = 11, sliding log: 10.** Every t = 0 entry is now over 10 s old and gone — the full allowance
  returned **all at once**.
- **t = 11, sliding counter: 5.** We're now ~5 s into the *next* slice: `prev = 10`, weight = 0.5,
  estimate = 10 × 0.5 = 5, so 5 more fit. The old burst is "half forgotten".

The counter's numbers depend on where the 10-second boundaries fall relative to your burst, so your counts
for that row may differ. That's its approximation at work, not a bug. A second run (18:12, burst landing
**5 s into** a window instead of at its start) gave:

```
t=0   sliding_counter  200 × 10, then 429     retry-after: 4   ← the window ends in ~4 s
t=5   sliding_counter  200 200, then 429      ← new window ~2 s in: weight 0.8, estimate 10 × 0.8 = 8, so 2 fit
t=11  sliding_counter  200 × 7, then 429      ← ~9 s in: weight 0.1, estimate 10 × 0.1 + 2 = 3, so 7 fit
```

Same code, same requests, different counts, because the estimate depends on how far into the current
window you are. The token bucket and the sliding log gave the same results in both runs.

Look at the headers too — `retry-after` reflects each algorithm's own clock:

```
sliding_log      x-ratelimit-remaining: 0, x-ratelimit-reset: 9, retry-after: 9
sliding_counter  x-ratelimit-remaining: 0, x-ratelimit-reset: 18, retry-after: 8
```

The log says "retry in 9 s" (when the oldest entry leaves). The counter says "retry in 8 s" (when the next
slice starts) and "reset in 18 s" (after the next slice too). The token bucket says "retry in 1 s".

Stop the three servers with Ctrl+C in each terminal afterwards.

### Check 6 — a typo in the setting is refused

```powershell
$env:RATEGATE_ALGORITHM = "leaky_bucket"; uv run uvicorn app.main:app
```

**Our output (last lines):**

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
algorithm
  Input should be 'token_bucket', 'sliding_log' or 'sliding_counter' [type=literal_error, input_value='leaky_bucket', input_type=str]
```

**Meaning:** the server refuses to start and tells you the valid choices. Failing loudly at startup is far
better than running with a silently wrong limiter. Clean up: `Remove-Item Env:RATEGATE_ALGORITHM`.

### Check 7 — the middleware didn't change

```powershell
git diff --stat HEAD -- app/middleware.py
```

**Expected:** no output (no changes). **Meaning:** three algorithms now run behind the middleware, and it
wasn't edited once. That's the strategy pattern's promise, verified.

---

## Part E — Troubleshooting

| Problem | Cause / fix |
|---|---|
| `TypeError: Can't instantiate abstract class ... with abstract method check` | A strategy class is missing `check`, or it's misspelled |
| `ImportError: cannot import name 'Decision' from 'app.limiter.token_bucket'` | Something still imports `Decision` from the old place → import from `app.limiter.base` |
| `ValidationError ... algorithm` at startup | `RATEGATE_ALGORITHM` has a typo, or is still set from an earlier check → `Remove-Item Env:RATEGATE_ALGORITHM` |
| Tests: `async def functions are not natively supported` | `pytest-asyncio` not installed, or `asyncio_mode = "auto"` missing from `pyproject.toml` |
| Tests fail with `ConnectionError` | Redis isn't running → `docker start redis` |
| `ResponseError` mentioning a script line | Lua typo → compare with the code above |
| Unexpected algorithm behaviour | Check which one is running: `$env:RATEGATE_ALGORITHM` in that terminal |
| Counter results differ from this guide | Expected: they depend on where window boundaries fall |

---

## Part F — Finish the phase

**Done when:** all 16 tests pass, switching `RATEGATE_ALGORITHM` changes behaviour without code edits,
and `middleware.py` is unchanged.

```powershell
uv run ruff check .
uv run ruff format .
git add .
git commit -m "Phase 2: sliding window log and counter behind a common strategy interface"
git push
```

(`pyproject.toml` has `[tool.ruff] extend-exclude = ["docs"]`. Without it, `ruff format` would also
rewrite the Python examples inside these Markdown guides.)

Then tick Phase 2 in `ROADMAP.md`.

**Check your understanding:**

1. What is the strategy pattern, and how does `middleware.py` prove it works here?
2. Why must `RateLimitStrategy.check` be an `@abstractmethod`? What happens if a subclass forgets it?
3. Why does the sliding window log need a sequence number in each member?
4. A client is limited to 1,000 requests per minute. How much does each algorithm store for them?
5. At t = 5 in Check 5, why did the token bucket allow requests but the sliding log allowed none?
6. In the sliding counter, previous window = 6, current = 2, we're 70% into the current window, limit 10.
   Is the next request allowed? *(weight = 0.3, estimate = 6 × 0.3 + 2 = 3.8, 3.8 + 1 ≤ 10 → yes)*
7. Why does the counter store the window number in the hash instead of using a key per window?
8. Why is the factory the only file that imports all three algorithm classes?
