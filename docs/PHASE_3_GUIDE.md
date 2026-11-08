# Phase 3 Guide — Correctness Under Concurrency

**Goal of this phase:** prove, with tests, that RateGate never lets more requests through than the limit,
even when hundreds arrive at the same instant, and that it behaves correctly at the edges of time
(refills, window boundaries, expiry).

This proves the resume line *"using atomic Redis Lua scripts to prevent race conditions under concurrent
requests."* Until now that was a claim. After this phase it's a test that fails if the claim is false.

**What happened when this phase was built (2026-11-02):** the new tests found **two real bugs** in the
Phase 1–2 code. Under a burst of 200 simultaneous HTTP requests, the existing server answered **77 to 100 of
them with `500 Internal Server Error`**. And the sliding window counter's `Retry-After` was too short.
This guide walks you through finding and fixing both, the way it happened.

All code below was run against your Redis: 31 tests passed 3 runs in a row, the concurrency tests passed
10 runs in a row, and `ruff` reports no problems.

---

## Part A — Concepts (read before coding)

### 1. Why the Phase 2 tests can't prove "no race conditions"

Look at how Phase 2's tests send requests:

```python
results = [(await limiter.check(client_id)).allowed for _ in range(7)]
```

Each `await` **finishes** before the next `check` starts. The requests are one after another
(**sequential**). A race condition only happens when two operations **overlap**: both read before either
writes. Sequential tests can never create overlap, so they can never catch a race. Even a broken limiter
would pass them.

To test for races, the test itself must create overlap: many requests in flight at once.

### 2. Concurrency with `asyncio.gather`

```python
decisions = await asyncio.gather(*(limiter.check(client_id) for _ in range(200)))
```

- `limiter.check(client_id)` without `await` doesn't run yet. It creates a **coroutine**, a paused
  piece of work.
- `(... for _ in range(200))` makes 200 of them. The `*` unpacks them as 200 separate arguments.
- `asyncio.gather(...)` starts all 200 **at once** and waits until every one finishes. It returns their
  results in the original order.

What happens inside: each coroutine runs until its first `await` (sending a command to Redis), then pauses,
and the next one starts. Within a few milliseconds, all 200 commands are on their way to Redis at the same
time. That's real overlap, just like 200 users clicking at the same moment.

**Concurrency vs parallelism:** this is *concurrency* (many tasks in progress at once, taking turns on one
thread), not *parallelism* (several CPUs literally working at the same instant). For race conditions,
concurrency is enough: what matters is that the read of one request can happen before the write of another.

### 3. Testing the test: a "negative control"

Suppose the concurrency test passes. How do you know it's capable of failing? Maybe the requests didn't
really overlap, and it would pass for broken code too. A test that can't fail proves nothing.

Scientists solve this with a **control**: run the same experiment on something you *know* is broken and
check that the experiment detects it. We do the same: `NaiveCounter` is a deliberately broken limiter that
reads, decides and writes as **separate** Redis calls (exactly the design the Lua script exists to avoid).
The test asserts that it lets **more than** 100 through.

Result when this phase was built: the naive limiter let **200 of 200** through, every time. The Lua
version let exactly **100**. Same test harness, same Redis, same 200 concurrent requests; the only
difference is atomicity. That's the evidence.

### 4. Connection pools

Opening a network connection to Redis takes time (a handshake). So a client keeps a **pool** of open
connections and lends one to each command. When the command finishes, the connection goes back into the
pool for the next one.

What happens when all connections are busy? It depends on the pool:

| Pool | When all connections are busy | redis-py default size |
|---|---|---|
| `ConnectionPool` (the default) | **Raises `MaxConnectionsError`** immediately | 100 |
| `BlockingConnectionPool` | **Waits** (up to `timeout` seconds) for one to be returned | 50 |

Phases 1–2 used the default pool. With 200 requests at once, request 101 found all 100 connections busy and
**crashed**. The middleware didn't catch that error, so the user got `500 Internal Server Error`. That's
bug 1. The fix is a blocking pool: request 101 waits a few milliseconds for a connection instead of failing.

**Why only 50 connections is plenty:** Redis runs one command at a time anyway, and each takes
microseconds. 50 connections can carry thousands of requests per second. More connections would only use
more memory on both sides. (Redis itself accepts up to 10,000 connections by default, so in Phase 5,
3 servers × 50 = 150 is far within its limit.)

### 5. Simulating several servers in one test

In Phase 5, three separate server processes will each have their own Redis client. A test can imitate that
by creating several independent clients (each with its own pool) and spreading requests across them:

```python
limiters[i % 4].check(client_id)    # request 0 → limiter 0, 1 → 1, 2 → 2, 3 → 3, 4 → 0, ...
```

`i % 4` is the remainder of dividing by 4: it cycles 0, 1, 2, 3, 0, 1, … (round robin, like nginx).
If the limit holds across four independent clients, it will hold across four servers.

(Interesting detail: this test passed even **before** the pool fix. With 4 clients, each only had 50 of the
200 requests, below the default pool's limit of 100. The single-client test is what exposed the crash.)

### 6. Boundary testing

Bugs cluster at **boundaries**: the edges where behaviour changes. For a rate limiter, the edges are in time:

| Boundary | Question | Test |
|---|---|---|
| The moment a blocked client may retry | Is `Retry-After` honest? | Wait exactly `retry_after`, then the next request must be allowed |
| The moment an idle key expires | Does memory get cleaned up, on schedule? | Check the TTL, wait it out, the key must be gone |
| A window boundary | Can a client double its burst across it (the fixed-window loophole)? | 5 requests just before a boundary + 5 just after must total 5 allowed |

### 7. The second bug: the counter's `Retry-After`

The Phase 2 counter, when the **current** window alone was full, said "retry when this window ends".
But in the next window the old count becomes `prev`, still weighted almost 100% at the window's start.

Example: limit 10, window 10 s. A client makes 10 requests 3 s into a window.

- **Old answer:** retry in 7 s (when the window ends).
- **What actually happens at that moment:** `prev = 10`, weight = (10 − 0) / 10 = 1, estimate = 10. Still full. **Blocked.**
- **Correct answer:** 7 s, plus the time in the next window for 10 × weight to fall to 9 (to fit 1 more):
  weight must be ≤ 0.9, which happens 1 s in. Retry in **8 s**.

Formula, for "this window alone is full": wait = (window − elapsed) + window × (1 − (limit − cost) / curr).

A client that obeys `Retry-After` should never be refused. The old code broke that promise, and the
boundary test caught it.

### 8. Test first, then fix ("red, green")

In this phase you'll write each test **before** fixing the bug it targets, run it, and **watch it fail**
("red"). Then fix the code and watch it pass ("green"). Why bother seeing it fail?

- It proves the test actually detects the bug. A test you've only seen pass might be testing nothing.
- It proves your fix is what made the difference.

This habit is called **test-driven development (TDD)**. You don't have to use it everywhere, but for bugs
it's the standard: first a failing test that reproduces the bug, then the fix.

---

## Part B — Setup

Nothing to install: `pytest`, `pytest-asyncio` and `httpx` are already in your dev dependencies.

Make sure Docker Desktop is open and Redis runs: `docker start redis`.

---

## Part C — Build it (in this order)

Files touched in this phase (★ new, ✎ changed):

```
app/
  config.py                         ✎ Step 3 (two pool settings)
  redis_client.py                   ★ Step 3
  main.py                           ✎ Step 3
  limiter/scripts/sliding_counter.lua ✎ Step 6 (retry fix)
  middleware.py                       (unchanged)
tests/
  conftest.py                       ✎ Step 3
  test_concurrency.py               ★ Step 1
  test_limiters.py                  ✎ Step 5 (boundary tests appended)
scripts/
  concurrent_burst.py               ★ Step 8
README.md                           ✎ Step 9
```

### Why this order

| Step | What | Why here |
|---|---|---|
| 1 | Concurrency tests | Write the proof first, before touching app code |
| 2 | Run them → **red** | See what's actually broken (you'll find the pool crash) |
| 3 | Fix the pool | Fix only what the failing tests point at |
| 4 | Run them → **green** | Confirm the fix, and that the negative control now behaves |
| 5 | Boundary tests → **red** | Same pattern for the time edges (you'll find the counter bug) |
| 6 | Fix the counter | Fix only what failed |
| 7 | Run everything, repeatedly | A concurrency test must pass *every* time, not once |
| 8 | HTTP burst script | Prove it end to end, through the real server |
| 9 | README + commit | Record it |

---

### Step 1 — `tests/test_concurrency.py` (new)

```python
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
```

> **Note:** this file imports `create_redis` from `app.redis_client`, which you'll create in Step 3. For
> Step 2's red run, temporarily replace `create_redis(settings)` with
> `Redis.from_url(settings.redis_url, decode_responses=True)` and remove that import line, so you can see
> the original failure. Put them back in Step 3.

**What's happening:**

- **`LIMIT = 100`, `ATTEMPTS = 200`**: constants in capitals, named once, used everywhere. Twice the
  limit means half must be refused: an exact, easy-to-check expectation.
- **Settings chosen so nothing refills mid-test:** the token bucket gets `refill_rate=0.001` (one token
  every ~17 minutes), and the windows are 60 s. If tokens could drip back during the test, the right answer
  might be 101, and the test would need a vague range. Removing that effect makes the expected result
  **exactly** 100.
- **`NaiveCounter`**: the broken design on purpose. `get` → decide in Python → `set` are three separate
  network round trips; 200 overlapping requests all `get` "0" before anyone `set`s, so nearly all are
  allowed. `px=60_000` = expire in 60,000 ms (the `_` is just a readable thousands separator in Python).
  Its key prefix `rategate:naive:` keeps it apart from the real algorithms. It lives in the test file,
  never in `app/`, because it's a testing tool, not a feature.
- **`sum(d.allowed for d in decisions)`**: in Python, `True` counts as 1 and `False` as 0, so summing the
  booleans counts the allowed requests.
- **`test_limit_holds_across_separate_instances`** doesn't use the `redis` fixture: it makes its own four
  clients. `try` / `finally` guarantees they're closed even if the test fails (otherwise connections leak).
- **The three tests together** say: one server is safe, several servers are safe, and the test would have
  caught it if they weren't.

### Step 2 — Run them and watch them fail (red)

```powershell
uv run pytest tests/test_concurrency.py -v
```

**Output when this phase was built (before any fix):**

```
test_simultaneous_requests_never_exceed_limit[token_bucket] FAILED
test_simultaneous_requests_never_exceed_limit[sliding_log] FAILED
test_simultaneous_requests_never_exceed_limit[sliding_counter] FAILED
test_limit_holds_across_separate_instances[token_bucket] PASSED
test_limit_holds_across_separate_instances[sliding_log] PASSED
test_limit_holds_across_separate_instances[sliding_counter] PASSED
test_naive_limiter_lets_too_many_through FAILED
...
E   redis.exceptions.MaxConnectionsError: Too many connections
```

**How to read it:** the failures aren't "too many allowed". They're **crashes**:
`MaxConnectionsError`. One client with 200 simultaneous commands exceeded the default pool's 100
connections (Concept 4). The four-client test passed because each client only had 50. Even the naive
limiter's test crashed before it could show anything.

**Why this matters beyond tests:** your real server has one Redis client. 200 simultaneous users would hit
the same crash, and the middleware turns it into `500 Internal Server Error`. You'll see this for real in
Check 4.

### Step 3 — Fix it: a blocking connection pool

**3a. `app/config.py`**: add two settings under `redis_url`:

```python
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50  # connections shared by all requests
    redis_pool_timeout: float = 5.0  # seconds to wait for a free connection
    algorithm: Algorithm = "token_bucket"
```

- `redis_max_connections`: the pool size (Concept 4 explains why 50).
- `redis_pool_timeout`: the longest a request will wait for a free connection before giving up. Waiting
  forever would let requests pile up silently if Redis became slow. 5 s is generous for a wait that is
  normally a few milliseconds. (What happens when it *does* give up is Phase 4's topic.)
- As settings, both can be changed per environment: `RATEGATE_REDIS_MAX_CONNECTIONS=100`.

**3b. `app/redis_client.py`** (new): one place that builds the Redis client:

```python
from redis.asyncio import BlockingConnectionPool, Redis

from app.config import Settings


def create_redis(settings: Settings) -> Redis:
    # A blocking pool makes a burst of requests wait briefly for a free connection
    # instead of failing with "Too many connections".
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_timeout,
        decode_responses=True,
    )
    return Redis.from_pool(pool)
```

- `BlockingConnectionPool.from_url(...)` builds a pool that **waits** instead of raising. It takes the
  same URL as before, plus the pool size and wait time. `decode_responses=True` moves here from `main.py`.
- `Redis.from_pool(pool)` creates a client that **owns** the pool: when the client is closed
  (`await redis.aclose()`), it closes the pool and all its connections too. (Using `Redis(connection_pool=pool)`
  instead would leave the pool open after the client closes.)
- **Why a separate file?** Both the app and the tests need a Redis client, and they must be built the same
  way. If tests built Redis differently from the app, they could pass while the app fails (that's exactly
  what happened: tests and app both used the default pool, but nothing tested a burst). One function, used
  by both, keeps them identical. It also takes `Settings` as a parameter (dependency injection again).

**3c. `app/main.py`**: use it. Replace the `from redis.asyncio import Redis` import with
`from app.redis_client import create_redis`, and the client line in `lifespan`:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.limiter.factory import create_limiter
from app.middleware import RateLimitMiddleware
from app.redis_client import create_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = create_redis(settings)
    app.state.limiter = create_limiter(redis, settings)
    yield
    await redis.aclose()
```

(The rest of the file is unchanged.) `main.py` no longer imports anything from `redis` directly.

**3d. `tests/conftest.py`**: make the test fixture build Redis the same way as the app:

```python
from uuid import uuid4

import pytest

from app.config import settings
from app.redis_client import create_redis


@pytest.fixture
async def redis():
    client = create_redis(settings)
    yield client
    await client.aclose()


@pytest.fixture
def client_id():
    # A fresh id per test, so tests never share a bucket or wait for old keys to expire.
    return f"test-{uuid4()}"
```

**3e.** If you used the temporary `Redis.from_url` lines in Step 1, restore `create_redis(settings)` and its
import in `tests/test_concurrency.py`.

### Step 4 — Run again (green)

```powershell
uv run pytest tests/test_concurrency.py -v
```

Expected: `7 passed`. All three algorithms now give exactly 100 of 200, on one client and across four,
and the naive limiter's test passes. That means it **did** let more than 100 through (it lets all 200
through). The broken design is detectably broken; the atomic design isn't.

### Step 5 — Boundary tests (append to `tests/test_limiters.py`)

Add these to the **end** of `tests/test_limiters.py` (they reuse its `ALGORITHMS` and `make`):

```python
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
```

**What's happening:**

- **`test_waiting_retry_after_is_enough`** checks the promise in the `Retry-After` header: exhaust the
  limit, take the blocked decision, sleep exactly `retry_after` (+ 50 ms margin for timing noise), and the
  next request **must** be allowed. Runs for all three algorithms.
- **`EXPIRY`** gives each algorithm settings where its key should live exactly 1 s (token bucket:
  capacity 5 ÷ refill 5/s = 1 s; log: 1 window) or 2 s (counter: 2 windows), plus its key prefix so the test
  can find the key in Redis.
- **`test_key_expires_when_idle`**: after one request, `pttl` (milliseconds left) must be positive and no
  more than the expected lifetime; after waiting that long, `exists` must return 0 (the key is gone).
  This proves idle clients don't leak memory.
- **`sleep_until`**: reads **Redis's** clock (`redis.time()` returns seconds and microseconds, the same
  `TIME` the Lua scripts use), works out how far into the current 1-second window it is (`%` = remainder),
  and sleeps until the requested offset. The second `% window_ms` wraps around: if we're at 950 and want 50,
  it sleeps 100 ms into the next window. Using Redis's clock matters: the scripts decide windows by it.
- **`test_no_double_burst_across_window_boundary`**: 5 requests 100 ms before a boundary, 5 just after.
  A naive fixed-window limiter would allow all 10 (Phase 2, the "fixed window loophole"). The log (exact)
  and the counter (weighted) must allow exactly 5 in total.

Run them:

```powershell
uv run pytest tests/test_limiters.py -v
```

**Output when this phase was built (red):**

```
test_waiting_retry_after_is_enough[token_bucket] PASSED
test_waiting_retry_after_is_enough[sliding_log] PASSED
test_waiting_retry_after_is_enough[sliding_counter] FAILED
...
>       assert (await limiter.check(client_id)).allowed
E       assert False
```

The counter's client waited exactly as long as told, and was **still refused**. That's bug 2 (Concept 7).
The expiry and boundary tests passed: those parts were already right.

### Step 6 — Fix the counter's retry time

In `app/limiter/scripts/sliding_counter.lua`, replace the `else` branch of the decision (the part that
computes `retry_after_ms`). **Before:**

```lua
  local room = limit - cost - curr
  if room < 0 or prev == 0 then
    -- This window alone is full: wait for the next one.
    retry_after_ms = window_ms - elapsed
  else
    -- Wait until the previous window's share shrinks enough.
    retry_after_ms = math.ceil(window_ms * (1 - room / prev) - elapsed)
  end
```

**After:**

```lua
  local room = limit - cost - curr
  if room >= 0 then
    -- Wait until the previous window's share shrinks enough.
    retry_after_ms = math.ceil(window_ms * (1 - room / prev) - elapsed)
  else
    -- This window alone is full. In the next one its count becomes the previous
    -- count, so also wait for that share to shrink enough.
    local next_wait = 0
    if curr > 0 then
      next_wait = math.max(0, window_ms * (1 - (limit - cost) / curr))
    end
    retry_after_ms = math.ceil(window_ms - elapsed + next_wait)
  end
```

**What changed and why:**

- **The two cases are swapped and the condition simplified** to `room >= 0` vs not. Why is `prev == 0`
  no longer needed? If `prev` were 0, the estimate would just be `curr`, and a blocked request means
  `curr + cost > limit`, so `room` is negative. So `room >= 0` already implies `prev > 0`, and the division
  by `prev` is safe.
- **Case "this window alone is full"** now adds `next_wait`: in the next window, the current count becomes
  `prev` at weight ≈ 1, and the request only fits once `curr × weight + cost ≤ limit`, i.e.
  weight ≤ (limit − cost) / curr. That weight is reached `window × (1 − (limit − cost) / curr)` into the
  next window. With the example from Concept 7: 10 × (1 − 9/10) = 1 s.
- **`if curr > 0`** guards against dividing by zero. It can only be 0 if a single request costs more than
  the whole limit, which never happens today, but a division by zero inside Redis would be an ugly failure.
- **`math.max(0, ...)`** keeps the extra wait from ever being negative.

Nothing else in the script changes.

### Step 7 — Run everything, repeatedly

```powershell
uv run pytest -v
```

Expected: **31 passed** (16 from Phase 2 + 8 boundary + 7 concurrency), in about 17 seconds (the boundary
tests sleep on purpose).

A concurrency test that passes once might be lucky. Run them 10 times:

```powershell
foreach ($i in 1..10) { uv run pytest -q tests/test_concurrency.py | Select-Object -Last 1 }
```

Expected: `7 passed` ten times.

### Step 8 — `scripts/concurrent_burst.py` (new): the end-to-end check

The tests call the limiter directly. This script goes through **everything**: HTTP, Uvicorn, middleware,
pool, Redis. It's also the tool you'll reuse in Phase 5 against three servers behind nginx.

Create the folder `scripts` at the project root (next to `app` and `tests`), then:

```python
"""Fire many requests at a running RateGate server at the same moment and count the answers."""

import argparse
import asyncio
from collections import Counter
from uuid import uuid4

import httpx


async def burst(url: str, total: int, api_key: str) -> Counter:
    limits = httpx.Limits(max_connections=total)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        responses = await asyncio.gather(
            *(client.get(url, headers={"X-API-Key": api_key}) for _ in range(total))
        )
    return Counter(response.status_code for response in responses)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/resource")
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--api-key", default=f"burst-{uuid4().hex[:8]}")
    args = parser.parse_args()

    counts = asyncio.run(burst(args.url, args.total, args.api_key))
    print(f"{args.total} requests at once, API key {args.api_key}:")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
```

**What's happening:**

- **The docstring** (first line in quotes) describes the script, and `argparse` reuses it as the
  `--help` text (`description=__doc__`).
- **`httpx.AsyncClient`** is an async HTTP client, the HTTP equivalent of the async Redis client.
  `httpx.Limits(max_connections=total)` lets it open one connection per request, so all 200 really are
  simultaneous (by default httpx would cap it at 100, which would quietly make it a smaller burst).
  `timeout=30` stops it hanging forever if the server stalls.
- **`async with ... as client`**: the client is closed automatically when the block ends.
- **`asyncio.gather`** fires all requests at once, exactly as in the tests.
- **`Counter`** (from `collections`) counts how many times each value occurs:
  `Counter([200, 429, 200])` → `{200: 2, 429: 1}`.
- **`argparse`** reads command-line options: `--url`, `--total`, `--api-key`, each with a default.
  `type=int` converts the text "200" to the number 200.
- **The default API key is random** (`uuid4().hex[:8]` = 8 random hex characters), so every run is a
  brand-new client: no waiting for old state to expire.
- **`asyncio.run(...)`** starts an event loop for the async function: the entry point from normal code
  into async code. (In tests, pytest-asyncio does this for you.)
- **`if __name__ == "__main__":`** runs `main()` only when the file is executed directly, not when
  another file imports it.

### Step 9 — README

Add a **Testing** section to `README.md`, e.g.:

```markdown
## Testing

`uv run pytest` runs 31 tests against a real Redis, including:

- **Concurrency:** 200 simultaneous requests against a limit of 100 allow exactly 100, for every
  algorithm, from one client and from four independent clients. A deliberately non-atomic limiter in the
  same test lets all 200 through, showing the test can detect a race.
- **Boundaries:** waiting the returned `Retry-After` is always enough; idle keys expire on schedule; no
  double burst across a window boundary.

End to end against a running server: `uv run python scripts/concurrent_burst.py --total 200`.
```

---

## Part D — Verify (checks, with real outputs)

**Timing:** none of these checks needs waiting between runs. The tests use a random client per test, and
the burst script uses a random API key per run.

### Check 1 — The full suite, 3 times

```powershell
foreach ($i in 1..3) { uv run pytest -q | Select-Object -Last 1 }
```

**Our output:**

```
31 passed in 17.12s
31 passed in 16.62s
31 passed in 17.79s
```

**Meaning:** every Phase 1–3 behaviour holds. ~17 s is mostly deliberate sleeps in the boundary and recovery tests.

### Check 2 — The concurrency tests, 10 times

```powershell
foreach ($i in 1..10) { uv run pytest -q tests/test_concurrency.py | Select-Object -Last 1 }
```

**Our output:** `7 passed` ten times (3.2–3.9 s each).

**Meaning:** the roadmap's "done when": the concurrency test passes **repeatedly**. Races are about timing,
which varies run to run. Ten clean runs make luck very unlikely.

### Check 3 — The negative control, in numbers

The naive test only asserts "more than 100". To see the actual numbers, this was run 5 times (same 200
concurrent requests, limit 100, fresh client each time):

```
naive allowed 200/200   token bucket allowed 100/200
naive allowed 200/200   token bucket allowed 100/200
naive allowed 200/200   token bucket allowed 100/200
naive allowed 200/200   token bucket allowed 100/200
naive allowed 200/200   token bucket allowed 100/200
```

**Meaning:** without atomicity, the limit **didn't hold at all**: every overlapping request read "0" before
anyone wrote. With the Lua script, exactly 100. This is the single most convincing piece of evidence in
the project. (Use it in interviews: "I wrote a non-atomic version as a control; it let 200 of 200 through.")

### Check 4 — End to end over HTTP, before and after

Start the server with a limit of 100 and (almost) no refill. In terminal 1:

```powershell
$env:RATEGATE_CAPACITY = "100"; $env:RATEGATE_REFILL_RATE = "0.01"; uv run uvicorn app.main:app
```

Why `REFILL_RATE=0.01`: at the default 1 token/s, a burst taking half a second could earn one more token,
making 101 a correct answer and the result harder to read.

In terminal 2:

```powershell
uv run python scripts/concurrent_burst.py
```

**Our output, the old (Phase 2) code vs the fixed code**, 3 runs each, 200 requests at once, limit 100:

```
=== run 1
OLD code:    200: 100   500: 100
FIXED code:  200: 100   429: 100
=== run 2
OLD code:    200: 100   429: 23   500: 77
FIXED code:  200: 100   429: 100
=== run 3
OLD code:    200: 100   429: 23   500: 77
FIXED code:  200: 100   429: 100
```

**Meaning:**

- **Old code:** the 100 allowed were correct, but **77–100 of the refused requests crashed with
  `500 Internal Server Error`** instead of getting a polite 429. That's bug 1 as a real user would see it.
  How many crash varies from run to run; that any do is the bug.
- **Fixed code:** exactly 100 allowed and 100 told "429, retry later", every time.

The same check with the other algorithms (stop the server, Ctrl+C, then):

```powershell
Remove-Item Env:RATEGATE_CAPACITY, Env:RATEGATE_REFILL_RATE
$env:RATEGATE_LIMIT = "100"; $env:RATEGATE_WINDOW_SECONDS = "60"; $env:RATEGATE_ALGORITHM = "sliding_log"; uv run uvicorn app.main:app
```

**Our output:**

```
sliding_log:      200: 100   429: 100
sliding_counter:  200: 100   429: 100
```

Clean up afterwards: `Remove-Item Env:RATEGATE_LIMIT, Env:RATEGATE_WINDOW_SECONDS, Env:RATEGATE_ALGORITHM`.

### Check 5 — Lint, and the middleware is still untouched

```powershell
uv run ruff check .
uv run ruff format --check .
git diff --stat HEAD -- app/middleware.py
```

**Expected:** `All checks passed!`, `... files already formatted`, and no output from `git diff`.

---

## Part E — Common pitfalls

| Pitfall | Symptom | Why | Fix |
|---|---|---|---|
| Testing concurrency with a loop of `await`s | Tests pass even for broken code | Sequential requests never overlap | Use `asyncio.gather` |
| No negative control | A passing test you can't trust | Nothing proves the test can fail | Keep `NaiveCounter` and its test |
| `MaxConnectionsError: Too many connections` | 500 errors under a burst | Default pool raises past 100 connections | `BlockingConnectionPool` via `create_redis` |
| Tests build Redis differently from the app | Tests pass, production fails | Two different setups | Both call `create_redis(settings)` |
| Refill during the concurrency test | 101 allowed instead of 100, sometimes | Tokens dripped back mid-burst | `refill_rate=0.001` in tests, `RATEGATE_REFILL_RATE=0.01` for the HTTP check |
| Boundary test using Python's clock | Flaky boundary test | Python's and Redis's clocks differ slightly; windows follow Redis's | `sleep_until` reads `redis.time()` |
| No margin on timing tests | Rare random failures | Scheduling and network add a few ms | `+ 0.05` / `+ 0.1` seconds |
| Forgetting `try` / `finally` around extra clients | Warnings about unclosed connections | A failed assert skips the cleanup | `finally:` closes them regardless |
| httpx capping the burst | Burst script shows fewer simultaneous requests than asked | httpx defaults to 100 connections | `httpx.Limits(max_connections=total)` |
| Leftover `RATEGATE_*` variables | Later runs use limit 100 unexpectedly | `$env:` lasts until the terminal closes | `Remove-Item Env:...` or a new terminal |
| Running a test once and calling it proven | A race slips through later | Timing varies run to run | Repeat concurrency tests (Check 2) |

---

## Part F — Finish the phase

**Done when:** `uv run pytest` is green (31 passed), the concurrency tests pass 10 runs in a row, and the
HTTP burst gives exactly 100 × `200` and 100 × `429`.

```powershell
git add .
git commit -m "Phase 3: concurrency and boundary tests; fix connection pool crash and counter Retry-After"
git push
```

Then tick Phase 3 in `ROADMAP.md`.

**Check your understanding:**

1. Why can't Phase 2's tests detect a race condition, even for a broken limiter?
2. What does `asyncio.gather` do differently from a loop of `await`s?
3. What is a negative control, and what did ours show?
4. Why did the single-client concurrency test crash, but the four-client test pass, before the fix?
5. What's the difference between `ConnectionPool` and `BlockingConnectionPool` when all connections are busy?
6. Why do the app and the tests both build Redis through `create_redis`?
7. Limit 10, window 10 s, 10 requests made 3 s into a window. What should `Retry-After` be, and why
   was the old answer (7 s) wrong? *(8 s: in the next window the 10 still count at weight ≈ 1; it takes
   1 more second for 10 × weight to fall to 9.)*
8. Why does the concurrency test use `refill_rate=0.001` instead of the default?
9. Why does `sleep_until` use Redis's clock instead of Python's?
10. Why write a failing test **before** fixing a bug?
