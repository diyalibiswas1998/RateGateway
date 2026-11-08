# Phase 4 Guide — Failure Handling (Fail-Open / Fail-Closed)

**Goal of this phase:** decide, on purpose, what RateGate does when Redis can't be reached — and make it
notice quickly, answer sensibly, and recover by itself when Redis comes back.

This proves the resume line *"Added configurable fail-open/fail-closed handling for Redis outages."*

**What the current (Phase 3) code does today when Redis has a problem** — measured on 2026-11-02:

| Redis problem | What a user gets now |
|---|---|
| Stopped (`docker stop redis`) | `500 Internal Server Error`, taking up to **4 s** per request |
| Hung (`docker pause redis`) | Request **freezes for 5 s**, then `500 Internal Server Error` |

(For reference: a plain redis-py client with *all* default settings took **27 s** to fail one command against
a closed port. Phase 3's pool already avoided the worst of that; Phase 4 tunes it properly.)

Every request depends on Redis, so right now a Redis problem becomes an outage of your whole API — slow
*and* broken. After this phase: answers in about half a second (or 1–2 s, see Concept 3), with a clear,
deliberate response, and automatic recovery.

All code below was run against your Redis: 36 tests passed 3 runs in a row, the concurrency tests passed
10 runs in a row, and every scenario in Part D was performed for real with `docker stop` / `docker pause`.

---

## Part A — Concepts (read before coding)

### 1. Three ways Redis can fail

| Failure | Real-world cause | How it looks to the app | How we simulate it |
|---|---|---|---|
| **Down** | Redis crashed, was restarted, network refuses the connection | Connecting fails | `docker stop redis` |
| **Hung / slow** | Redis overloaded, network problem, machine frozen | Connected, but no reply ever comes | `docker pause redis` (freezes the container) |
| **Restarted** | Redis came back after being down | Connections in the pool are dead ("stale") until replaced | `docker restart redis` |

Each needs something different: *down* must be detected fast, *hung* needs a limit on how long we wait for a
reply, and *restarted* needs old connections replaced automatically.

### 2. Fail-open vs fail-closed

When the limiter can't check, there are only two honest choices:

| Mode | What happens | Protects | Risks | Typical users |
|---|---|---|---|---|
| **Fail-open** | Let the request through, unchecked | **Availability**: your API keeps working | No rate limiting during the outage | Public APIs, websites, read-only endpoints |
| **Fail-closed** | Refuse the request with `503` | **Protection**: no unlimited traffic, ever | Your API is down while Redis is | Login, payments, password reset, expensive endpoints |

Neither is "correct" — it depends on what's worse for *your* API: a few minutes without limits, or a few
minutes without service. That's why it's a **setting** (`RATEGATE_FAIL_MODE`), not hard-coded. The default
is **open**: for most APIs, the limiter failing shouldn't take down the service it protects.

**Why `503`, not `429`?** `429 Too Many Requests` means *"you* are over your limit". That's false here — the
client did nothing wrong. `503 Service Unavailable` means *"the server* can't handle this right now", which is
the truth. Clients and monitoring tools treat them differently (a 429 tells a client to slow down; a 503 tells
an operator something is broken). We add `Retry-After: 5` so well-behaved clients wait instead of hammering.

**In fail-open, no `X-RateLimit-*` headers** are sent: we don't know the real numbers, and making them up
would mislead clients.

### 3. Timeouts — two of them, chosen by measurement

A **timeout** is the longest you'll wait for something before giving up. Without one, a hung Redis freezes
every request (Phase 3 code: 5 s, the library default). Redis connections have two kinds of waiting:

- **Connect timeout** — opening a new connection.
- **Socket (command) timeout** — waiting for the reply to a command on an open connection.

They need **different** values, because they take very different times. Measured on this machine:

| What | Median | Slowest |
|---|---|---|
| 1 new connection | 17 ms | 18 ms |
| 50 new connections at once | ~110 ms | ~155 ms |
| 200 new connections at once | ~430 ms | **~600 ms** |
| A command on an open connection | 0.4 ms | 1.25 ms |

- **Commands are fast and consistent** → a tight timeout is safe: **0.25 s** (about 200× the slowest command).
- **Connecting can take 600 ms during a burst** → it needs room: **1.0 s**. Connections are pooled, so new
  ones are rare and this generous value rarely matters in normal operation.

*(First attempt at this phase used 0.1 s for both. The concurrency tests — which open 200 connections at
once — then failed with `Timeout connecting to server`. In production, that would have meant a traffic spike
right after startup **falsely switching on outage mode**. Measuring first, then choosing, avoided that.)*

**Windows detail:** when nothing listens on a port, Windows takes about **2 seconds** to report "connection
refused" (Linux normally reports it immediately). So on your machine, while Redis is stopped, each request
waits for the connect timeout — about 1–2 s — before fail-open/closed applies. When Redis is *hung*, the
command timeout applies instead — about 0.5 s.

### 4. Retries — exactly one, immediately

redis-py's client retries failed commands several times with growing pauses (exponential backoff). During an
outage that's the worst possible behaviour: we measured **27 seconds** for one request to fail against a closed
port with the library's defaults.

But zero retries has a problem too. After Redis restarts, connections sitting in the pool are dead. The first
command on each fails with "Connection closed by server", even though Redis is fine again. One immediate retry
(`Retry(NoBackoff(), retries=1)`) opens a fresh connection and succeeds. Measured after `docker restart redis`:
**first request 0.01 s, allowed** — the user never notices.

So: **one retry, no waiting.** Enough to replace a stale connection; not enough to delay noticing a real
outage. (Worst case per request = 2 × the relevant timeout.)

### 5. Where to handle the failure: the middleware

redis-py raises exceptions from a family tree:

```
RedisError
├── ConnectionError          (can't connect, connection closed)
│   └── MaxConnectionsError  (Phase 3's pool error)
└── TimeoutError             (connect or reply took too long)
```

Catching **`RedisError`** catches all of them. The middleware is the right place, because the decision is
about the **HTTP response** (pass through, or `503`) — that's the middleware's job. The algorithms stay
unchanged: they don't care what HTTP does with their failures.

This is the first change to `middleware.py` since Phase 1. That's fine: Phase 2 kept it unchanged because
*algorithms* were changing. Phase 4 changes *HTTP behaviour*, which belongs exactly there.

### 6. Logging

When the limiter is bypassed or refusing, the operator must know. Python's `logging` module:

```python
logger = logging.getLogger("rategate")
logger.warning("Redis unavailable, allowing request (fail-open): %s", error)
```

- `getLogger("rategate")` gets a named logger for our app.
- **Levels** say how serious it is: `warning` (degraded but serving — fail-open) vs `error` (refusing users — fail-closed).
- `%s` and the extra argument: logging fills in the error text *only if* the message is actually printed (cheaper than an f-string).

Compare: the Phase 3 code printed a **full error traceback** for every failed request — 59 lines for a handful
of requests. The new code prints one readable line each.

### 7. Testing outages without breaking Docker

Stopping Docker inside automated tests would be slow, would break other tests running at the same time, and
wouldn't work in CI. Instead, tests point a Redis client at a port where **nothing listens** (`6399`). Every
command fails exactly as it does when Redis is down. The real `docker stop` / `docker pause` checks are done
by hand, in Part D.

The tests send real HTTP requests into the app using **`httpx.ASGITransport`** — httpx talks to the FastAPI app
directly in memory (no server, no network), but the request still goes through the real middleware. The test
sets `app.state.limiter` and `app.state.fail_mode` itself, to choose a working or broken Redis per request.

### 8. A known limit, and the next improvement (not built here)

During a full outage on Windows, *every* request pays the connect timeout (1–2 s). Large systems add a
**circuit breaker**: after several failures in a row, stop trying Redis for a few seconds and apply the fail
mode instantly, then test Redis again. Like a household fuse that trips and is reset later. It's a natural
Phase 8 extension; you can mention it in interviews as "what I'd add next".

---

## Part B — Setup

Nothing to install. Make sure Docker Desktop is open and Redis runs (`docker start redis`).

**Important:** Part C step 1 and Part D stop and pause your Redis container on purpose. Always finish with
`docker start redis` / `docker unpause redis` and check `docker exec redis redis-cli ping` → `PONG`.

---

## Part C — Build it (in this order)

Files touched (★ new, ✎ changed):

```
app/
  config.py           ✎ Step 3 (fail mode + two timeouts)
  redis_client.py     ✎ Step 4 (timeouts + one retry)
  middleware.py       ✎ Step 5 (catch Redis errors, apply fail mode)
  main.py             ✎ Step 6 (one line)
tests/
  test_fail_modes.py  ★ Step 2
README.md             ✎ Step 8
```

| Step | What | Why here |
|---|---|---|
| 1 | See the problem live | Know exactly what you're fixing |
| 2 | Write `test_fail_modes.py` → **red** | Prove the tests detect today's behaviour |
| 3 | Settings | Everything else reads them |
| 4 | Timeouts + retry in `create_redis` | Detect failure fast, recover from restarts |
| 5 | Middleware outage handling | Decide the HTTP answer |
| 6 | `main.py` | Hand the fail mode to the middleware |
| 7 | Run → **green**, then everything repeatedly | Confirm, and check nothing else broke |
| 8 | README | Record it |

### Step 1 — See the problem live (current code)

Terminal 1:

```powershell
uv run uvicorn app.main:app
```

Terminal 2 (`-w` makes curl print the status code and how long it took; `-m 15` gives up after 15 s):

```powershell
curl.exe -s -o NUL -m 15 -w "%{http_code} in %{time_total}s\n" http://127.0.0.1:8000/api/resource
docker stop redis
curl.exe -s -o NUL -m 15 -w "%{http_code} in %{time_total}s\n" http://127.0.0.1:8000/api/resource
curl.exe -s -o NUL -m 15 -w "%{http_code} in %{time_total}s\n" http://127.0.0.1:8000/api/resource
docker start redis
docker pause redis
curl.exe -s -o NUL -m 15 -w "%{http_code} in %{time_total}s\n" http://127.0.0.1:8000/api/resource
docker unpause redis
docker exec redis redis-cli ping
```

**What we saw:** `200` while up; `500` while stopped (up to 4.1 s); `500` after a **5.0 s freeze** while paused;
and terminal 1 filled with error tracebacks. Stop the server (Ctrl+C).

### Step 2 — `tests/test_fail_modes.py` (new) — then watch it fail

```python
import time

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
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
    app.state.limiter = limiter
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
    # Worst case: the attempt and its one retry each wait the full connect timeout.
    budget = 2 * settings.redis_connect_timeout + 0.5
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
```

**What's happening:**

- **`DOWN_URL`** — port 6399 has nothing listening, so a client built with it behaves like Redis is down.
- **`redis_down` fixture** — builds that client with the app's own `create_redis`, so it uses the same
  timeouts and retry as production. `Settings(redis_url=DOWN_URL)` creates a settings object with only that
  one value changed (all others keep their defaults).
- **`call_api`** — the HTTP harness. It plugs the chosen limiter and fail mode into `app.state`, then sends
  real requests through the app with `httpx.ASGITransport` (no server needed). It returns a list of
  responses. Using the real app means the **middleware itself** is tested, not just a function.
- **`[response] = await call_api(...)`** — unpacking a one-item list: it also asserts there's exactly one item.
- **The five tests:**

| Test | Proves |
|---|---|
| `fail_open_allows...` | Redis down + open → `200`, and no made-up rate-limit headers |
| `fail_closed_rejects...` | Redis down + closed → `503` with `Retry-After: 5` |
| `outage_is_detected_within_the_timeouts` | Failure is noticed within the configured worst case (2 × connect timeout + margin), computed **from the settings**, not a magic number |
| `limiting_resumes_after_an_outage` | Before: 1 token used (`200`). During: `503`, `503` — and the outage doesn't consume tokens. After: the 2nd token (`200`), then empty (`429`). Limiting picks up exactly where it left off |
| `fail_mode_accepts_only_open_or_closed` | `"closed"` is accepted; anything else is refused at startup |

- `time.perf_counter()` is a precise stopwatch: the difference between two readings is the elapsed seconds.

**Run it now, before any fix:**

```powershell
uv run pytest tests/test_fail_modes.py -v
```

**Output on the Phase 3 code:**

```
test_fail_open_allows_requests_when_redis_is_down FAILED
test_fail_closed_rejects_requests_when_redis_is_down FAILED
test_outage_is_detected_within_the_timeouts FAILED
test_limiting_resumes_after_an_outage FAILED
test_fail_mode_accepts_only_open_or_closed FAILED
```

Each outage test took ~2 s and crashed with the Redis error — exactly the `500` from Step 1.

**A lesson from building this:** the first version of the last test only checked that `Settings(fail_mode="maybe")`
raises an error. On the **old** code it **passed** — because the old `Settings` rejects *any* unknown setting
name, so it raised for the wrong reason. A test that passes on code without the feature tests nothing. Adding
`assert Settings(fail_mode="closed").fail_mode == "closed"` makes it fail on the old code, as it should. Always
check your new tests fail before the feature exists.

### Step 3 — `app/config.py`: fail mode and timeouts

Add a type alias next to `Algorithm`:

```python
Algorithm = Literal["token_bucket", "sliding_log", "sliding_counter"]
FailMode = Literal["open", "closed"]
```

and three settings after `redis_pool_timeout`:

```python
    redis_pool_timeout: float = 5.0  # seconds to wait for a free connection
    redis_connect_timeout: float = 1.0  # seconds to open a connection
    redis_socket_timeout: float = 0.25  # seconds to wait for a command's reply
    fail_mode: FailMode = "open"  # what to do when Redis can't be reached
    algorithm: Algorithm = "token_bucket"
```

- `FailMode` uses `Literal` like `Algorithm`: a typo such as `RATEGATE_FAIL_MODE=closd` stops the app at
  startup instead of silently behaving as "open".
- The two timeout values come from the measurements in Concept 3. As settings, they can be tuned per
  environment (e.g. a remote Redis in another data center needs more than 0.25 s).

### Step 4 — `app/redis_client.py`: timeouts and one retry

```python
from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.config import Settings


def create_redis(settings: Settings) -> Redis:
    # A blocking pool makes a burst of requests wait briefly for a free connection
    # instead of failing with "Too many connections".
    pool = BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_timeout,
        socket_connect_timeout=settings.redis_connect_timeout,
        socket_timeout=settings.redis_socket_timeout,
        # One immediate retry replaces a connection that went stale (e.g. Redis
        # restarted); more retries would only delay noticing a real outage.
        retry=Retry(NoBackoff(), retries=1),
        decode_responses=True,
    )
    return Redis.from_pool(pool)
```

**What changed:** three new arguments to the pool. Everything passed to `from_url` that the pool itself doesn't
use is handed to **every connection** it creates:

- `socket_connect_timeout` → each connection's connect timeout (1.0 s).
- `socket_timeout` → each connection's reply timeout (0.25 s).
- `retry=Retry(NoBackoff(), retries=1)` → each connection retries a failed command once, immediately.
  `NoBackoff()` means "wait 0 seconds before retrying". The `Retry` here is redis-py's own class — not something
  we wrote.

*(While building this, the first attempt called a `with_retry()` method on the client. That method doesn't
exist in redis-py — it came from memory, not the documentation. Checking the library's source showed the real
mechanism: a `retry` argument on connections. Lesson: verify an API exists before relying on it.)*

### Step 5 — `app/middleware.py`: catch Redis errors, apply the fail mode

At the top, add two imports, a constant and a logger:

```python
import logging
import math

from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

EXEMPT_PATHS = {"/health", "/docs", "/openapi.json"}
OUTAGE_RETRY_AFTER = "5"  # seconds a client should wait while Redis is unreachable

logger = logging.getLogger("rategate")
```

In `dispatch`, replace the single line `decision = await limiter.check(identify_client(request))` with:

```python
        limiter = request.app.state.limiter
        try:
            decision = await limiter.check(identify_client(request))
        except RedisError as error:
            if request.app.state.fail_mode == "closed":
                logger.error(
                    "Redis unavailable, rejecting request (fail-closed): %s", error
                )
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Rate limiter unavailable. Try again shortly."},
                    headers={"Retry-After": OUTAGE_RETRY_AFTER},
                )
            logger.warning("Redis unavailable, allowing request (fail-open): %s", error)
            return await call_next(request)
```

Everything after it (building headers, the 429, attaching headers) is unchanged.

**What's happening:**

- **`try` / `except RedisError as error`** — if `check` raises any Redis error (Concept 5), jump to the
  `except` block; `error` holds the exception so we can log its message.
- **Fail-closed** — log at `error` level, return `503` with a JSON body and `Retry-After: 5`. The route never runs.
- **Fail-open** — log at `warning` level and run the route normally with `call_next`, **without** any
  `X-RateLimit-*` headers (we don't know the real numbers).
- **`request.app.state.fail_mode`** — the mode is read from app state (set in Step 6), like the limiter.
  Reading it from state rather than importing `settings` lets tests switch modes per request.
- **`OUTAGE_RETRY_AFTER` is a string** because header values are text.
- **Only Redis errors are caught.** A bug in our own code (say, a `TypeError`) should still surface as a `500`
  — hiding real bugs behind fail-open would be dangerous.

### Step 6 — `app/main.py`: one line

In `lifespan`, after the limiter:

```python
    app.state.limiter = create_limiter(redis, settings)
    app.state.fail_mode = settings.fail_mode
    yield
```

### Step 7 — Run, then run everything repeatedly

```powershell
uv run pytest tests/test_fail_modes.py -v --durations=5
```

Expected: **5 passed**. `--durations=5` lists the 5 slowest tests: each outage test takes ~2 s on Windows
(connect timeout 1 s × 2 attempts against the closed port).

Then everything — the new timeouts must not break Phase 3's burst tests:

```powershell
foreach ($i in 1..3) { uv run pytest -q | Select-Object -Last 1 }
foreach ($i in 1..10) { uv run pytest -q tests/test_concurrency.py | Select-Object -Last 1 }
```

Expected: `36 passed` ×3 (about 29 s — the outage tests wait on purpose), `7 passed` ×10.

### Step 8 — README

Add a **Failure handling** section, e.g.:

```markdown
## Failure handling

If Redis can't be reached, `RATEGATE_FAIL_MODE` decides:

- `open` (default): requests are allowed without limiting, and a warning is logged.
- `closed`: requests get `503 Service Unavailable` with `Retry-After: 5`, and an error is logged.

Short timeouts (1 s to connect, 0.25 s per command) and a single immediate retry keep outages from
freezing requests, and limiting resumes by itself when Redis returns.
```

and add three rows to the configuration table:

```markdown
| `RATEGATE_FAIL_MODE` | `open` | All: `open` or `closed` when Redis is unreachable |
| `RATEGATE_REDIS_CONNECT_TIMEOUT` | `1.0` | All: seconds to open a Redis connection |
| `RATEGATE_REDIS_SOCKET_TIMEOUT` | `0.25` | All: seconds to wait for a Redis reply |
```

---

## Part D — Verify (checks, with real outputs)

### Check 1 — Tests

```
36 passed in 29.27s
36 passed in 28.55s
36 passed in 29.65s
```
and `7 passed` ten times for the concurrency tests.

### Check 2 — Live outages, both modes side by side

Run two servers, one per mode:

```powershell
# terminal 1
$env:RATEGATE_FAIL_MODE = "open"; uv run uvicorn app.main:app --port 8030
# terminal 2
$env:RATEGATE_FAIL_MODE = "closed"; uv run uvicorn app.main:app --port 8031
```

In terminal 3, for each scenario run a request against each server:

```powershell
curl.exe -s -i -m 15 -w "\n%{http_code} in %{time_total}s\n" http://127.0.0.1:8030/api/resource
curl.exe -s -i -m 15 -w "\n%{http_code} in %{time_total}s\n" http://127.0.0.1:8031/api/resource
```

…with these between scenarios: `docker stop redis` → (requests) → `docker start redis` → wait 2 s →
(requests) → `docker pause redis` → (requests) → `docker unpause redis` → (requests).

**Our results** (the Phase 3 code ran alongside on port 8032 for comparison):

| Scenario | Fail-open (8030) | Fail-closed (8031) | Phase 3 code (8032) |
|---|---|---|---|
| A. Redis up | `200`, 0.01 s, rate-limit headers | `200`, 0.01 s, rate-limit headers | `200`, 0.01 s |
| B. `docker stop redis` | `200`, 1–2 s, **no** rate-limit headers | `503`, 1–2 s, `retry-after: 5` | `500`, up to **4.1 s** |
| C. `docker start redis` (no app restart) | `200`, 0.02 s, headers back | `200`, 0.02 s, headers back | `200` |
| D. `docker pause redis` (hung) | `200` in **0.51–0.54 s** | `503` in **0.53–0.54 s** | `500` after a **5.04 s** freeze |
| E. `docker unpause redis` | `200`, 0.03 s, headers back | `200`, 0.02 s, headers back | `200` |

**Meaning:**

- **Stopped:** both new modes apply their policy. They take 1–2 s per request on Windows because a refused
  connection takes Windows ~2 s to report, so the 1 s connect timeout (× the one retry) is what ends the wait
  (Concept 3). The old code answers with an error.
- **Hung:** the 0.25 s command timeout × 2 attempts ≈ 0.5 s, instead of a 5 s freeze. Ten times faster, and
  a deliberate answer instead of a crash.
- **Recovery:** after `docker start` / `unpause`, the very next request is limited normally — no restart of
  RateGate. The one retry swapped the dead pooled connections for new ones.

*(In one run, fail-open answered in 0.05 s right after `docker stop`, while Redis was still shutting down.
After a few seconds of being stopped, both modes settled at 1–2 s. Report steady-state numbers, not the
lucky first one.)*

### Check 3 — The logs

Terminal 1 (fail-open) printed, per affected request:

```
Redis unavailable, allowing request (fail-open): Connection closed by server.
```

Terminal 2 (fail-closed):

```
Redis unavailable, rejecting request (fail-closed): Timeout connecting to server
```

The Phase 3 code printed `ERROR: Exception in ASGI application` followed by a full traceback each time —
59 log lines for the same handful of requests.

### Check 4 — Cleanup and lint

```powershell
docker exec redis redis-cli ping          # must say PONG
Get-ChildItem Env:RATEGATE_*              # should list nothing; remove leftovers with Remove-Item
uv run ruff check .
uv run ruff format --check .
```

---

## Part E — Common pitfalls

| Pitfall | Symptom | Why | Fix |
|---|---|---|---|
| No timeouts | Requests freeze 5 s (library default) while Redis hangs | Waiting for a reply that never comes | `socket_timeout` / `socket_connect_timeout` |
| One timeout for everything, set too low | Outage mode triggers during traffic spikes | Connecting takes far longer than a command under load | Separate connect (1.0 s) and command (0.25 s) timeouts, from measurements |
| Library-default retries with backoff | 27 s to fail one request | Several retries with growing pauses | `Retry(NoBackoff(), retries=1)` |
| Zero retries | Errors right after Redis restarts, though Redis is fine | Pooled connections are stale | One immediate retry |
| Using `429` when Redis is down | Clients think *they* did something wrong | 429 means "you're over your limit" | `503` + `Retry-After` |
| Sending made-up rate-limit headers in fail-open | Clients trust wrong numbers | We have no real numbers | Send no `X-RateLimit-*` headers |
| Catching `Exception` instead of `RedisError` | Real bugs hidden behind fail-open | Too broad | Catch only `RedisError` |
| Test passes on code without the feature | False confidence | Test checks something the old code also does | Run new tests on the old code first |
| Calling a method from memory (`with_retry`) | `AttributeError` | It doesn't exist in the library | Check the source or docs |
| Forgetting to restart Redis after a check | Everything fails/allows unlimited afterwards | Redis still stopped or paused | `docker start redis` / `docker unpause redis`, then `ping` |
| `RATEGATE_FAIL_MODE` left set in a terminal | A later server unexpectedly fails closed | `$env:` lasts until the terminal closes | `Remove-Item Env:RATEGATE_FAIL_MODE` |
| Judging by the first measurement | Misleadingly fast numbers | Transitional moments (Redis shutting down) | Measure steady state, repeat |

---

## Part F — Finish the phase

**Done when:** all 36 tests pass, and with `docker stop redis` the API still responds according to the
configured mode (roadmap's "done when").

```powershell
git add .
git commit -m "Phase 4: fail-open/fail-closed handling with short Redis timeouts and one retry"
git push
```

Then tick Phase 4 in `ROADMAP.md`.

**Check your understanding:**

1. Name the three ways Redis can fail and what each needs from the app.
2. When would you choose fail-closed over fail-open? Give one endpoint for each.
3. Why `503` and not `429` when Redis is down?
4. Why are the connect and command timeouts different (1.0 s vs 0.25 s), and how were they chosen?
5. What went wrong when both timeouts were 0.1 s?
6. Why exactly one retry, with no backoff?
7. Why does a stopped Redis take 1–2 s per request on Windows, but a hung one only ~0.5 s?
8. Why catch `RedisError` and not `Exception`?
9. Why does fail-open send no `X-RateLimit-*` headers?
10. What does `test_limiting_resumes_after_an_outage` prove about tokens during an outage?
11. What is a circuit breaker, and what problem would it solve here?
