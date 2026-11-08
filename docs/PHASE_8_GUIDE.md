# Phase 8 Guide — Polish: Tiers, Metrics, Architecture and Design Decisions

**Goal of this phase:** three finishing touches from the roadmap, each a feature real rate limiters have:

1. **Per-tier limits** — paid API keys get 10 times the free limits.
2. **`/metrics`** — counters a monitoring system can read: how many requests were allowed, limited, or let
   through / refused during a Redis outage, and how long each Redis check takes.
3. **An architecture diagram and a "design decisions" section** in the README — the project explained at a glance,
   with the *why* behind each choice.

Phases 1–7 each proved a claim from the resume line. Phase 8 is optional polish: it makes the project look and
behave like a production service, and gives you more to talk about in an interview.

**What we measured** (2026-11-03):

| Result | Evidence |
|---|---|
| Tiers work | Free key: `limit: 10`, `tier: free`; paid key: `limit: 100`, `tier: paid`. A burst of 200: free 10 allowed, paid 101 |
| Metrics are exact | Counters summed over the 3 copies matched what the clients received, to the request: free 11 allowed / 190 limited, paid 102 / 99 |
| Metrics cost nothing measurable | Phase 7's load test with metrics on: 230 req/s, p50 4 ms, p95 6 ms (before: 4 ms / 7 ms) |
| `/metrics` isn't public | Through nginx: `404` |
| Nothing broke | 47 tests (40 + 7 new) pass 3 runs in a row; CI's stack check: exactly 100 of 200 |

---

## Part A — Concepts (read before coding)

### 1. Tiers: different limits for different customers

Real APIs sell plans: a free plan with a small limit, paid plans with bigger ones. The rate limiter must apply
**each client's own** limit.

How RateGate does it:

- **Two tiers**, `free` and `paid`. The free limits are the existing settings (`capacity`, `refill_rate`, `limit`,
  `window_seconds`); the paid limits are new `paid_*` settings, 10 times bigger by default.
- **Which keys are paid** is a setting: `RATEGATE_PAID_API_KEYS`, a JSON list such as `'["key-1", "key-2"]'`.
  Everyone else — unknown keys and clients without a key — is free.
- **One limiter per tier.** At startup the app builds two limiters (same algorithm, different numbers) and keeps them
  in a dictionary, `app.state.limiters = {"free": ..., "paid": ...}`. The middleware looks up the client's tier and
  uses that tier's limiter.
- **Responses say which tier applied:** a new `X-RateLimit-Tier` header.

**In a real product**, the tier would come from a customer database (keys are issued when someone signs up and
pays). A setting keeps this project self-contained, and swapping it for a database lookup later changes only
`client_tier`.

**What happens when a customer upgrades?** Their Redis key stays the same (`rategate:tb:key:<their key>`); the paid
limiter simply checks it with the bigger numbers from the next request on. The scripts already cap tokens at the
current capacity, so nothing breaks.

### 2. Metrics: counting what the limiter does

So far, the only way to see what RateGate does is to read its logs or run a test. In production, operators watch
**metrics**: numbers the service keeps updating, collected every few seconds and drawn as graphs, with alerts when
something looks wrong ("50% of requests limited!", "fail-open happening!").

The standard tool is **Prometheus**. Each service publishes its numbers as plain text at `/metrics`; Prometheus
fetches ("scrapes") that page regularly. The official Python library, `prometheus-client`, does the bookkeeping.

**Three kinds of metric** (we use two):

| Kind | Meaning | RateGate |
|---|---|---|
| **Counter** | Only goes up: "how many so far" | `rategate_decisions_total` — decisions by outcome and tier |
| **Histogram** | Counts values in ranges ("buckets"): "how many took under 1 ms, under 2.5 ms …" | `rategate_check_seconds` — how long each Redis check took |
| Gauge | Goes up and down: "how many right now" | not needed |

**Labels** split one metric into series: `rategate_decisions_total{outcome="limited",tier="free"}`. Ours are
`outcome` (`allowed`, `limited`, `fail_open`, `fail_closed`) and `tier` (`free`, `paid`) — at most 8 series.
**Never label by API key or IP:** every distinct label value creates a new series, and millions of clients would
create millions of series and overwhelm the monitoring system. Labels must have a small, fixed set of values.

**What the page looks like** (a sample after 3 requests with a bucket of 2):

```
# HELP rategate_decisions_total Rate-limit decisions, by outcome (allowed, limited, fail_open, fail_closed)
# TYPE rategate_decisions_total counter
rategate_decisions_total{outcome="allowed",tier="free"} 2.0
rategate_decisions_total{outcome="limited",tier="free"} 1.0
# TYPE rategate_check_seconds histogram
rategate_check_seconds_bucket{le="0.001"} 0.0
rategate_check_seconds_bucket{le="0.005"} 3.0
...
rategate_check_seconds_bucket{le="+Inf"} 3.0
rategate_check_seconds_count 3.0
rategate_check_seconds_sum 0.004158700030529872
```

- `# HELP` and `# TYPE` lines describe each metric.
- A counter named `rategate_decisions` is published as `rategate_decisions_total` (the library adds `_total`).
- Histogram buckets are **cumulative**: `le="0.005"} 3.0` means 3 checks took **at most** 5 ms (`le` = "less than or
  equal"). Here none was under 1 ms and all 3 under 5 ms; together they took 4.2 ms (`_sum`).
- The library also prints `..._created` lines — the time each series started (seconds since 1970). Prometheus uses
  them to notice restarts; you can ignore them.

### 3. Metrics with several copies

Each API copy is a separate process with its **own** counters, and it only counts the requests **it** answered.
Behind round-robin, each copy sees about a third of the traffic. Two consequences:

- **A monitoring system reads every copy** and adds them up. (Prometheus finds the copies through service discovery;
  for us, a small script does the same: `scripts/collect_metrics.py`.)
- **`/metrics` through nginx would be misleading** — each request would reach a different copy and show a
  different third. And it would expose internal numbers to the public. So nginx answers `404` for `/metrics`, and
  monitoring reads the copies directly on the internal network.

Counters also **restart from 0** when a copy restarts — normal for Prometheus, which handles resets.

### 4. Where the counting happens

All decisions go through the middleware, so it's the one place to count:

| Situation | Counted as |
|---|---|
| Redis says allowed | `allowed` |
| Redis says over the limit (`429`) | `limited` |
| Redis unreachable, fail-open (`200`) | `fail_open` |
| Redis unreachable, fail-closed (`503`) | `fail_closed` |

The **timer** (`CHECK_SECONDS.time()`) wraps only the Redis call, so it measures the limiter's own cost — and it
records failed checks too (a timed-out check shows up as ~0.25 s, which is useful to see).

`/metrics` itself is **exempt** from rate limiting, like `/health`: a monitoring system that gets `429`s would go
blind exactly when traffic is high.

### 5. Architecture diagram and design decisions

A README that starts with a picture of the system and ends with "why it's built this way" lets a reviewer
understand the project in two minutes. GitHub draws diagrams written in **Mermaid**, a text format inside a
` ```mermaid ` code block:

```
flowchart LR
    clients["Clients"] -->|"HTTP, port 8080"| nginx["nginx"]
    nginx --> api1["API copy 1"]
    api1 --> redis[("Redis")]
```

- `flowchart LR` — a flowchart, left to right.
- `name["label"]` — a box; `name[("label")]` — a cylinder (the usual symbol for a database).
- `-->` an arrow, `-->|"text"|` a labelled arrow, `-.->` a dotted arrow. `<br/>` breaks a label onto two lines.

Because it's text, the diagram lives in git, changes with the code, and needs no drawing tool.

The **design decisions** table gathers the choices from every phase — what was decided, why, and the evidence —
so the reasoning isn't buried in commit messages.

### 6. Tests first — and in the right order

Seven new tests: 4 for tiers, 3 for metrics. Written first, they all fail on the Phase 7 code — but the metrics tests
fail early, because the middleware can't handle the new test setup (`app.state.limiters`) yet. Building **tiers
first** makes the tier tests pass, after which the metrics tests fail for the **right** reason (counters at 0,
`/metrics` returning `404`). Phase 4's lesson: a red test should be red because of the missing feature.

---

## Part B — Setup

Docker Desktop and your Phase 0 Redis running; your `.env` from Phase 5 in place. One new library:

```powershell
uv add prometheus-client
```

This adds `prometheus-client>=0.26.0` to `pyproject.toml`'s dependencies (the API needs it, so it's not a dev tool)
and updates `uv.lock`. The Docker image picks it up on the next `--build`.

---

## Part C — Build it (in this order)

Files touched (★ new, ✎ changed):

```
app/
  config.py              ✎ Step 2 (Tier type, paid_* settings)
  limiter/factory.py     ✎ Step 2 (a limiter per tier)
  middleware.py          ✎ Steps 2, 4 (tier lookup, header; counting and timing)
  main.py                ✎ Steps 2, 4 (limiters per tier; /metrics route)
  metrics.py             ★ Step 4
tests/
  test_tiers.py          ★ Step 1
  test_metrics.py        ★ Step 1
  test_fail_modes.py     ✎ Step 2 (harness: limiters per tier)
  test_instance_id.py    ✎ Step 2 (same)
nginx/nginx.conf         ✎ Step 5 (/metrics → 404)
docker-compose.yml       ✎ Step 5 (pass RATEGATE_PAID_API_KEYS)
scripts/collect_metrics.py ★ Step 5
README.md                ✎ Step 6
ROADMAP.md               ✎ Step 7
```

| Step | What | Why here |
|---|---|---|
| 1 | Tier and metrics tests → red | Prove they detect the missing features |
| 2 | Tiers: settings, factory, middleware, `main.py`, two test harnesses → tier tests green | Metrics' tests depend on the new state layout |
| 3 | Run the metrics tests → red, for the right reason | Phase 4's lesson |
| 4 | Metrics: `metrics.py`, counting, timing, `/metrics` → green | |
| 5 | Stack: hide `/metrics` in nginx, pass paid keys, `collect_metrics.py`; check live | Prove it on the real three copies |
| 6 | README: architecture, tiers, metrics, design decisions, settings | Explain the whole project |
| 7 | Everything + ROADMAP | Nothing older broke |

### Step 1 — The tests, red

**`tests/test_tiers.py`:**

```python
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
```

- **`send`** — the usual in-memory harness, now setting `app.state.limiters` (a dict by tier) and
  `app.state.paid_api_keys`. `headers = {...} if api_key else {}` lets one test send **no** key.
- **`tiered`** — a free bucket of 1 and a paid bucket of 3, nothing refilling (`0.001`), so the expected codes are exact.
- **Test 1** — the same app, two keys: the free key gets `[200, 429]`, the listed paid key `[200, 200, 200, 429]`,
  and every response's `x-ratelimit-tier` says which. `{... for r in free}` is a **set**, so `== {"free"}` means
  "every response said free".
- **Test 2** — a client with **no** key is free even when paid keys exist. Keyless clients are identified by IP;
  httpx's in-memory client reports `127.0.0.1`, and the test first deletes that bucket so earlier runs can't affect it.
- **Test 3** — `monkeypatch.setenv` sets an environment variable for this test only. pydantic-settings reads a
  `set[str]` setting from JSON text, so `'["key-a", "key-b"]'` becomes `{"key-a", "key-b"}`.
- **Test 4** — the factory gives each tier its own numbers (with the sliding log, `limit` 10 vs `paid_limit` 100).

**`tests/test_metrics.py`:**

```python
import httpx
from prometheus_client import REGISTRY

from app.config import Settings
from app.limiter.token_bucket import TokenBucket
from app.main import app
from app.redis_client import create_redis


def decisions(outcome, tier="free"):
    """The current value of one decision counter (0 if it hasn't been used yet)."""
    labels = {"outcome": outcome, "tier": tier}
    return REGISTRY.get_sample_value("rategate_decisions_total", labels) or 0.0


def checks():
    return REGISTRY.get_sample_value("rategate_check_seconds_count") or 0.0


async def send(limiter, api_key, times, path="/api/resource", fail_mode="open"):
    app.state.limiters = {"free": limiter, "paid": limiter}
    app.state.paid_api_keys = set()
    app.state.fail_mode = fail_mode
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-API-Key": api_key}
        return [await client.get(path, headers=headers) for _ in range(times)]


async def test_allowed_and_limited_requests_are_counted(redis, client_id):
    allowed, limited, timed = decisions("allowed"), decisions("limited"), checks()
    await send(TokenBucket(redis, capacity=1, refill_rate=0.001), client_id, times=3)
    assert decisions("allowed") - allowed == 1
    assert decisions("limited") - limited == 2
    assert checks() - timed == 3  # every check of Redis was timed


async def test_fail_open_and_fail_closed_are_counted(client_id):
    down = create_redis(Settings(redis_url="redis://127.0.0.1:6399/0"))
    try:
        limiter = TokenBucket(down, 10, 1.0)
        opened, closed = decisions("fail_open"), decisions("fail_closed")
        await send(limiter, client_id, times=1, fail_mode="open")
        await send(limiter, client_id, times=1, fail_mode="closed")
    finally:
        await down.aclose()
    assert decisions("fail_open") - opened == 1
    assert decisions("fail_closed") - closed == 1


async def test_metrics_endpoint_is_never_limited(redis, client_id):
    limiter = TokenBucket(redis, capacity=1, refill_rate=0.001)
    responses = await send(limiter, client_id, times=5, path="/metrics")
    assert [r.status_code for r in responses] == [200] * 5
    assert "rategate_decisions_total" in responses[0].text
    assert "rategate_check_seconds" in responses[0].text
```

- **`REGISTRY.get_sample_value(name, labels)`** — reads one current value from the library's registry (where every
  metric lives). It returns `None` for a series that hasn't been used yet; `or 0.0` turns that into 0.
- **Measure the change, not the total.** Counters are shared by the whole test run (other tests also send
  requests), so each test reads the value **before**, acts, and checks the **difference**.
- **Test 1** — a bucket of 1, three requests: +1 `allowed`, +2 `limited`, and 3 more timed checks.
- **Test 2** — a client pointed at the dead port 6399 (Phase 4), once with fail-open and once with fail-closed: one
  of each counted. `try` / `finally` closes the client even if a check fails.
- **Test 3** — `/metrics` five times with a bucket of 1: all `200` (never limited), and the page contains both metrics.

Run both files on the Phase 7 code:

```powershell
uv run pytest tests/test_tiers.py tests/test_metrics.py -q
```

Ours: **7 failed** — `'Settings' object has no attribute 'paid_api_keys'`, `create_limiter() takes 2 positional
arguments but 3 were given`, and `'State' object has no attribute 'limiter'` (the old middleware reads a single
`app.state.limiter`, which the new harness no longer sets).

### Step 2 — Tiers

**`app/config.py`** — a type next to the others:

```python
FailMode = Literal["open", "closed"]
Tier = Literal["free", "paid"]
```

and after the sliding-window settings:

```python
    # Paid tier: these API keys get the paid_* limits instead (10 times the free ones).
    # From the environment as JSON, e.g. RATEGATE_PAID_API_KEYS='["key-1", "key-2"]'
    paid_api_keys: set[str] = set()
    paid_capacity: int = 100
    paid_refill_rate: float = 10.0
    paid_limit: int = 100
    paid_window_seconds: float = 10.0
```

- **`set[str]`** — a set of strings: checking "is this key paid?" is instant however many keys there are. Empty by
  default: no paid keys until you list some.
- The paid numbers mirror the free ones, times 10.

**`app/limiter/factory.py`:**

```python
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
```

- **`tier: Tier = "free"`** — a new parameter with a default, so existing calls (`create_limiter(redis, settings)`,
  e.g. in Phase 2's tests) still work unchanged.
- **`x if paid else y`** — Python's one-line choice: pick the paid number if this is the paid tier.
- The algorithm choice below is unchanged; it just uses the chosen numbers.

**`app/middleware.py`** — a function after `identify_client`:

```python
def client_tier(request: Request) -> str:
    # Only API keys listed as paid get the paid tier; everyone else, keyless included, is free.
    api_key = request.headers.get("X-API-Key")
    return "paid" if api_key in request.app.state.paid_api_keys else "free"
```

(A missing header gives `None`, and `None in a_set` is simply `False` — so keyless clients are free with no special case.)

In `dispatch`, the single limiter becomes a lookup:

```python
        tier = client_tier(request)
        limiter = request.app.state.limiters[tier]
```

and the headers gain one entry:

```python
            "X-RateLimit-Tier": tier,
```

**`app/main.py`** — in `lifespan`, the one limiter becomes two:

```python
    app.state.limiters = {
        "free": create_limiter(redis, settings, "free"),
        "paid": create_limiter(redis, settings, "paid"),
    }
    app.state.paid_api_keys = settings.paid_api_keys
```

**The two existing test harnesses** — in `tests/test_fail_modes.py` (`call_api`) and `tests/test_instance_id.py`
(`get`), replace `app.state.limiter = limiter` with:

```python
    app.state.limiters = {"free": limiter, "paid": limiter}
    app.state.paid_api_keys = set()
```

They set up the app the way `main.py` now does; what they test doesn't change.

```powershell
uv run pytest tests/test_tiers.py -q
```

Ours: **4 passed**.

### Step 3 — Metrics tests, red for the right reason

```powershell
uv run pytest tests/test_metrics.py -q
```

Ours: **3 failed** — `assert (0.0 - 0.0) == 1` twice (nothing is counted yet) and
`[404, 429, 429, 429, 429] == [200, 200, 200, 200, 200]` (no `/metrics` route, and it's rate-limited).

### Step 4 — Metrics

**`app/metrics.py` (new):**

```python
from prometheus_client import Counter, Histogram

# Each API copy keeps its own counts; a monitoring system reads every copy's /metrics
# and adds them up.
DECISIONS = Counter(
    "rategate_decisions",  # published as rategate_decisions_total
    "Rate-limit decisions, by outcome (allowed, limited, fail_open, fail_closed)",
    ["outcome", "tier"],
)

CHECK_SECONDS = Histogram(
    "rategate_check_seconds",
    "Time spent asking Redis whether a request may go ahead, in seconds",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
```

- **`Counter(name, description, labels)`** — creates the counter and registers it with the library. The description
  becomes the `# HELP` line.
- **`["outcome", "tier"]`** — the label names; values are given each time with `.labels(...)`.
- **`Histogram(..., buckets=...)`** — the upper edges of the time ranges, in seconds: 1 ms to 1 s. Our checks normally
  take 1–5 ms; timeouts land near 0.25 s or 1 s, so the buckets cover both.
- These are created **once**, when the module is first imported. Creating the same metric twice is an error
  (`Duplicated timeseries`), which is why they live in their own module.

**`app/middleware.py`** — the import, `/metrics` in the exempt paths, and counting:

```python
from app.metrics import CHECK_SECONDS, DECISIONS

EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/metrics"}
```

```python
        try:
            with CHECK_SECONDS.time():
                decision = await limiter.check(identify_client(request))
        except RedisError as error:
            if request.app.state.fail_mode == "closed":
                DECISIONS.labels("fail_closed", tier).inc()
                ...
            DECISIONS.labels("fail_open", tier).inc()
            logger.warning("Redis unavailable, allowing request (fail-open): %s", error)
            return await call_next(request)

        DECISIONS.labels("allowed" if decision.allowed else "limited", tier).inc()
```

- **`with CHECK_SECONDS.time():`** — starts a stopwatch, and when the block ends (normally **or** with an error)
  records the elapsed time in the histogram. `await` inside the block is fine: the stopwatch measures wall-clock time.
- **`DECISIONS.labels("fail_closed", tier).inc()`** — picks the series for these label values (in the order the labels
  were declared) and adds 1.
- One `inc()` on every path out of the check: allowed, limited, fail-open, fail-closed.

**`app/main.py`** — the route:

```python
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
```

```python
@app.get("/metrics")
async def metrics():
    # This copy's counters, in the text format Prometheus reads.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

- **`generate_latest()`** — the whole registry as Prometheus text (bytes).
- **`CONTENT_TYPE_LATEST`** — the content type Prometheus expects (`text/plain; version=1.0.0; charset=utf-8`).
- **`Response(...)`** — returns the text as-is (a normal route would turn its return value into JSON).

```powershell
uv run pytest tests/test_tiers.py tests/test_metrics.py -q
```

Ours: **7 passed**.

### Step 5 — The stack

**`nginx/nginx.conf`** — inside `server { ... }`, before `location / {`:

```nginx
    # Each copy's counters are for monitoring inside the network, not for the public.
    location = /metrics {
        return 404;
    }
```

- **`location = /metrics`** — `=` means **exactly** this path, and an exact match takes priority over `location /`.
- **`return 404;`** — nginx answers by itself; the request never reaches a copy.

**`docker-compose.yml`** — one more pass-through setting under the API's `environment:`:

```yaml
      RATEGATE_PAID_API_KEYS:
```

**`scripts/collect_metrics.py` (new):**

```python
"""Read /metrics from every API copy and add up the rate-limit decisions.

Each copy counts only the requests it answered, and /metrics isn't reachable through
nginx, so this asks each container directly, the way a monitoring system would.
"""

import subprocess
from collections import Counter

from prometheus_client.parser import text_string_to_metric_families

# Runs inside a container: fetch that copy's own /metrics.
FETCH = (
    "import urllib.request; "
    "print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode())"
)


def api_containers() -> list[str]:
    result = subprocess.run(
        ["docker", "compose", "ps", "api", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(result.stdout.split())


def decisions(container: str) -> Counter:
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", FETCH],
        capture_output=True,
        text=True,
        check=True,
    )
    counts = Counter()
    for family in text_string_to_metric_families(result.stdout):
        if family.name == "rategate_decisions":
            for sample in family.samples:
                if sample.name == "rategate_decisions_total":
                    key = (sample.labels["tier"], sample.labels["outcome"])
                    counts[key] += int(sample.value)
    return counts


def main() -> None:
    total = Counter()
    for container in api_containers():
        counts = decisions(container)
        total += counts
        summary = ", ".join(f"{t} {o}: {n}" for (t, o), n in sorted(counts.items()))
        print(f"{container}  {summary or 'nothing yet'}")
    summary = ", ".join(f"{t} {o}: {n}" for (t, o), n in sorted(total.items()))
    print(f"All copies  {summary or 'nothing yet'}")


if __name__ == "__main__":
    main()
```

- **`subprocess.run([...], capture_output=True, text=True, check=True)`** — runs a command and captures its output
  as text; `check=True` raises an error if the command fails.
- **`docker compose ps api --format "{{.Name}}"`** — the container names of the API service, one per line.
- **`docker exec <container> python -c FETCH`** — runs a one-line Python program **inside** each copy, fetching its own
  `http://localhost:8000/metrics` — no port needs publishing.
- **`text_string_to_metric_families`** — the library's parser for the Prometheus text format; each family's
  `samples` are the individual lines.
- **`Counter`** (Python's, from `collections`) — `+=` adds counts key by key, giving the total over all copies.

**Check it live**, with one paid key:

```powershell
$env:RATEGATE_PAID_API_KEYS = '["demo-paid"]'
docker compose up --build -d --wait
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8080/metrics
curl.exe -s -D - -o NUL -H "X-API-Key: demo-free" http://127.0.0.1:8080/api/resource
curl.exe -s -D - -o NUL -H "X-API-Key: demo-paid" http://127.0.0.1:8080/api/resource
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200 --api-key burst-free
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200 --api-key demo-paid
uv run python scripts/collect_metrics.py
Remove-Item Env:RATEGATE_PAID_API_KEYS
```

(`'...'` single quotes in PowerShell keep the double quotes inside the JSON as they are.)

Ours:

```
404
x-ratelimit-limit: 10      x-ratelimit-remaining: 9     x-ratelimit-tier: free
x-ratelimit-limit: 100     x-ratelimit-remaining: 99    x-ratelimit-tier: paid
  200: 10      429: 190            (burst, free key)
  200: 101     429: 99             (burst, paid key)
rategate-api-1  free allowed: 5, free limited: 61, paid allowed: 36, paid limited: 32
rategate-api-2  free allowed: 5, free limited: 63, paid allowed: 34, paid limited: 32
rategate-api-3  free allowed: 1, free limited: 66, paid allowed: 32, paid limited: 35
All copies  free allowed: 11, free limited: 190, paid allowed: 102, paid limited: 99
```

**Reading it:**

- `/metrics` → `404` through nginx.
- The free key gets 10, the paid key 100, and each response says its tier.
- Paid burst: 101 allowed — 99 tokens left after the single request, plus about 2 refilled during the burst
  (the paid tier refills 10 per second).
- **The counters match what the clients saw, exactly:** free 1 + 10 = **11** allowed and **190** limited; paid
  1 + 101 = **102** allowed and **99** limited. Each copy saw about a third — which is why monitoring must add them up.

**Does counting slow it down?** Phase 7's mixed test, rerun with metrics on: 230 requests/s, p50 4 ms, p95 6 ms —
the same as without. Counting is a few additions in memory; the Redis round trip dominates.

### Step 6 — README

- **Architecture** right after the introduction: the Mermaid diagram and a 5-step "how one request is checked".
- **Tiers** and **Metrics** sections after Failure handling.
- **Design decisions** before Configuration: a table of decision / why / evidence, gathered from every phase.
- The Testing count (47) and three bullets; `X-RateLimit-Tier` in the introduction; five `RATEGATE_PAID_*` rows in the
  Configuration table.

(Ours is in the project's README — the diagram code is there in full.)

### Step 7 — Everything, and the ROADMAP

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
foreach ($i in 1..3) { uv run pytest -q | Select-Object -Last 1 }
```

Ours: `All checks passed!`, `47 passed` three times (about 27–30 s). And the CI stack check, locally:
`200: 100`, `429: 100`, exit code 0. Then tick the three Phase 8 items in `ROADMAP.md`.

---

## Part D — Verify

| Check | How | Expected |
|---|---|---|
| 1. Tests | `uv run pytest -q` | `47 passed` |
| 2. Tiers | Step 5's `curl` lines | `tier: free` with limit 10; `tier: paid` with limit 100 |
| 3. Metrics hidden | `curl` to `:8080/metrics` | `404` |
| 4. Metrics exact | Bursts + `collect_metrics.py` | Totals equal what the clients received |
| 5. No slowdown | Phase 7's mixed Locust run | p50 4 ms, p95 6–7 ms |
| 6. Diagram | The README on GitHub | The flowchart draws (not an error box) |
| 7. CI | Push, Actions tab | Both jobs green |

---

## Part E — Common pitfalls

| Pitfall | Symptom | Why | Fix |
|---|---|---|---|
| Labelling metrics by API key or IP | Monitoring slows down or runs out of memory | Every label value is a new series | Only labels with a few fixed values (`outcome`, `tier`) |
| Reading `/metrics` through the load balancer | Numbers jump around between requests | Each request reaches a different copy | Read each copy; add them up |
| Exposing `/metrics` publicly | Internal numbers visible to anyone | It's served like any route | nginx `location = /metrics { return 404; }` |
| Rate-limiting `/metrics` | Monitoring goes blind under heavy traffic | It's a request like any other | Add it to `EXEMPT_PATHS` |
| Creating a metric twice | `Duplicated timeseries in CollectorRegistry` | Each name can be registered once | Define metrics once, in their own module |
| Checking counter totals in tests | Tests fail depending on what ran before | Counters are shared by the whole test run | Compare before and after |
| Forgetting the tier in a new code path | Some decisions missing from metrics | One `inc()` per exit path | Count on every path: allowed, limited, fail-open, fail-closed |
| `RATEGATE_PAID_API_KEYS=key-1` (not JSON) | Startup error from pydantic | A `set[str]` is read as JSON | `'["key-1"]'` |
| Double quotes around the JSON in PowerShell | The quotes inside disappear | PowerShell processes `"..."` | Single quotes outside: `'["key-1"]'` |
| Expecting metrics to survive a restart | Counters back at 0 | They live in the process's memory | Normal; Prometheus handles resets |
| A red test for the wrong reason | Metrics tests fail on `app.state` errors | Tiers not built yet | Build tiers first (Concept 6) |

---

## Part F — Finish the phase

**Done when:** `/metrics` counts allowed / limited / fail-open (and fail-closed); the README has an architecture
diagram and a design-decisions section; paid keys get their own limits — and the 47 tests and CI pass.

```powershell
git add app tests nginx/nginx.conf docker-compose.yml scripts/collect_metrics.py pyproject.toml uv.lock README.md ROADMAP.md docs/PHASE_8_GUIDE.md
git commit -m "Phase 8: per-tier limits, Prometheus metrics, architecture and design decisions"
git push
```

**Ideas beyond the roadmap** (not built): a **circuit breaker** (Phase 4: after repeated Redis failures, skip Redis
for a few seconds and apply the fail mode instantly); a real Prometheus and Grafana in the Compose file to draw the
metrics; tiers looked up in a database; per-route limits (e.g. stricter on `/login`).

**Check your understanding:**

1. How does RateGate decide a client's tier, and where would that come from in a real product?
2. Why does `create_limiter` take `tier` with a default value?
3. What's the difference between a counter and a histogram? Give one RateGate metric of each.
4. Why must you never label a metric by API key?
5. Why does each copy have its own counters, and how do you get the total?
6. Why does nginx hide `/metrics`, and why is `/metrics` exempt from rate limiting?
7. What does `rategate_check_seconds_bucket{le="0.005"} 3.0` mean?
8. Why do the metrics tests compare values before and after, instead of checking totals?
9. Why were tiers built before metrics?
10. How was it shown that the metrics are exact, and that they cost nothing measurable?
