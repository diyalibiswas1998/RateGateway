# Phase 5 Guide — Horizontal Scaling (3 API copies behind nginx)

**Goal of this phase:** run RateGate the way it would run in production — several identical copies of the
API behind a load balancer, all sharing one Redis — and prove that a client's limit holds **once, globally**,
no matter which copy answers.

This proves the resume's headline claim: a **distributed**, **horizontally scaled** rate limiter.

**What you will have at the end:**

| Piece | What it is |
|---|---|
| `Dockerfile` | The recipe that packages the API into an image |
| `docker-compose.yml` | One command starts Redis, 3 copies of the API and nginx |
| `nginx/nginx.conf` | The load balancer: hands requests to the copies in turn |
| `X-Instance-Id` header | Every response says which copy answered |
| `scripts/sequential_requests.py` | Shows the copies taking turns while sharing one count |
| `scripts/concurrent_burst.py` | Now also reports the answers per copy |

**What we measured with it** (2026-11-02, this machine):

| Experiment | Result |
|---|---|
| 14 requests one after another | Copies answered 1, 2, 3, 1, 2, 3 …; one shared count: 10 × `200`, then `429` |
| 200 requests at once, limit 100 | **Exactly 100** allowed, 3 runs out of 3, all three algorithms; each copy allowed about 33 |
| The same burst with each copy keeping its **own** state (a deliberately wrong setup) | **300** of 400 allowed — 100 per copy |
| One copy stopped while requests flow | 250 of 250 requests still answered `200`; slowest 1.01 s |
| Redis stopped, inside the containers | Fail-open `200` / fail-closed `503`, as configured |

**Two fixes to earlier phases come first.** Preparing this phase uncovered a real bug in Phase 4 (a retry
could count one request twice) and a flaky Phase 3 test. Both are explained in Concept 9 and fixed in Steps 1–2,
before any Docker work. All code below was run against your machine: the full suite (40 tests) passed 3 runs in a row, the concurrency tests passed 30 of 30 runs once this machine got its own looser timeouts (Concept 9b), and every check in Part D was run against the real stack.

---

## Part A — Concepts (read before coding)

### 1. Vertical vs horizontal scaling

When an API gets more traffic than one server can handle, there are two ways to grow:

| | Vertical ("scale up") | Horizontal ("scale out") |
|---|---|---|
| How | A bigger machine: more CPU, more memory | More machines (or processes) running the same code |
| Limit | The biggest machine you can buy | Practically none: add copies |
| One copy crashes | Everything is down | The others keep serving |
| Deploying new code | Downtime while it restarts | Replace copies one at a time |

Real APIs scale horizontally. But it only works if **any copy can answer any request** — the copies must
not keep important state in their own memory. A copy that keeps no state of its own is called **stateless**.

RateGate was designed for this from Phase 1: the counts live in **Redis**, not inside the API process. Phase 5
proves it with three copies.

### 2. Images, containers and the Dockerfile

You've used containers since Phase 0 (`docker run ... redis:7`). Two words to keep apart:

- An **image** is a packaged, read-only snapshot: an operating system's files, Python, our libraries, our
  code. Like a recipe that has already been cooked and frozen.
- A **container** is a running copy of an image. One image can run as many containers as you like — that's
  how we get 3 identical API copies.

A **Dockerfile** is the list of instructions that builds an image. Each instruction adds a **layer**:

| Instruction | Meaning |
|---|---|
| `FROM python:3.12-slim` | Start from an official image that already has Python 3.12 on a small Debian Linux |
| `COPY src dest` | Copy files from your project (or from another image) into the image |
| `WORKDIR /app` | "cd" into `/app` for all following instructions (created if missing) |
| `ENV NAME=value` | Set an environment variable, inside the image |
| `RUN command` | Run a command **while building** (e.g. install dependencies) |
| `EXPOSE 8000` | Document which port the app listens on (it doesn't publish it) |
| `CMD [...]` | The command a container runs **when it starts** |

**Layers are cached.** When you rebuild, Docker reuses every layer whose inputs haven't changed, and rebuilds
from the first one that did. That's why the Dockerfile copies `pyproject.toml` and `uv.lock` and installs the
dependencies **before** copying `app/`: changing your code then rebuilds only the last, tiny layer, instead of
reinstalling every library. Measured here: after a code change, the rebuild took **1.6 s** — every dependency layer was reported `CACHED`.

**`.dockerignore`** lists files Docker must not send to the build: `.venv` (Windows-specific, and huge),
`.git`, caches, tests, docs. Smaller, faster builds, and nothing private leaks into the image.

### 3. Docker Compose

Starting five containers by hand, each with the right settings and network, is error-prone. **Docker
Compose** reads one file, `docker-compose.yml`, that describes all of them as **services**, and starts them
with one command: `docker compose up`.

What Compose gives us:

- **One private network.** All services join it automatically.
- **Names instead of addresses.** On that network, each service is reachable by its **service name**: the API
  connects to `redis://redis:6379`, and nginx forwards to `api:8000`. Docker runs a small DNS server that
  turns names into container addresses.
- **Replicas.** `deploy: replicas: 3` starts three containers from the same service: `rategate-api-1`, `-2`, `-3`.
- **Health checks.** A command Docker runs every few seconds to ask "are you ready?" (`redis-cli ping`; a
  request to `/health`). `docker compose ps` shows `(healthy)`.
- **Start order.** `depends_on: condition: service_healthy` waits until a service is healthy: Redis before the
  API, the API before nginx.
- **Published ports.** Only nginx is published (`8080:80`: your PC's port 8080 → nginx's port 80). Redis and
  the API copies are reachable **only inside** the network. Clients must go through nginx.

**Two Redis servers now exist, on purpose.** Your `redis` container from Phase 0 stays on `localhost:6379` for
`uv run pytest` and the dev server. The Compose stack has its own Redis, not published, so the two never clash
and never share data.

**Settings pass through from your terminal.** In `docker-compose.yml`, `RATEGATE_CAPACITY:` with no value
means "copy this variable from the terminal that runs `docker compose`, if it's set there". So
`$env:RATEGATE_CAPACITY = "100"` before `docker compose up` changes the limit in all three copies, and when
it isn't set the app's defaults apply. (We checked: unset variables are not passed at all.)

### 4. nginx: a reverse proxy and load balancer

**nginx** (say "engine-x") is a fast, widely used web server. Here it plays two roles:

- **Reverse proxy:** clients talk to nginx; nginx forwards each request to an API copy and passes the answer
  back. Clients never see the copies.
- **Load balancer:** it spreads requests across the copies. The default strategy is **round-robin**: copy 1,
  copy 2, copy 3, copy 1, …

The configuration names a group of servers, an **upstream**:

```nginx
upstream rategate_api {
    zone rategate_api 64k;
    server api:8000;
}
```

`server api:8000` is one line, but when nginx starts it asks Docker's DNS for `api` and gets **three**
addresses — one per replica — so the group has three servers.

**Why the `zone` line matters (we found this the hard way).** Our first version had no `zone`. Every request
went to the **same** copy:

```
 1  200  instance=79524934a17f  remaining=9
 2  200  instance=79524934a17f  remaining=9
 ...
14  429  instance=79524934a17f  remaining=0
```

DNS did return all three addresses, so that wasn't it. The cause: nginx runs one **worker process** per CPU
core — 16 on this machine — and without `zone`, each worker keeps its **own** round-robin count, all starting
at the first server. Each new connection landed on a different worker, and each worker sent its first request
to copy 1. `zone` puts the count in memory shared by all workers. After adding it:

```
 1  200  517428e7f289  remaining=9
 2  200  400d48e5fba2  remaining=8
 3  200  c9e26d8f1474  remaining=7
 4  200  517428e7f289  remaining=6
```

### 5. Why the limit stays global — and how we proved it

Each copy runs its own Python process with its own memory. If a copy counted requests **in its own memory**,
a client could get the full limit **from each copy**: three copies, three times the limit. Behind a
round-robin load balancer, that happens automatically, without the client even trying.

RateGate's copies count **in Redis**, the one place they all share, with atomic Lua scripts (Phase 1, proved
in Phase 3). So a request to copy 2 sees the tokens copy 1 just used.

**The proof, both ways** — 200 or 400 requests at once, limit 100, through nginx:

| Setup | Allowed | Per copy |
|---|---|---|
| All 3 copies share one Redis (RateGate) | **100** of 200 | 34, 33, 33 |
| Each copy has its own separate store (a deliberately wrong setup, for comparison) | **300** of 400 | 100, 100, 100 |

The second row is the same kind of **negative control** as Phase 3's `NaiveCounter`: it shows the experiment
can tell right from wrong. (We built it by pointing each copy at a different Redis database number; you don't
need to repeat it.)

### 6. The client's IP address behind a proxy

Clients without an `X-API-Key` are identified by IP address (`identify_client`, Phase 1). Behind nginx there's
a catch: the API copies receive every connection **from nginx**, so `request.client.host` would be nginx's
address for **everyone** — all users would share one bucket.

The standard solution:

1. **nginx** adds a header with the real client address: `X-Forwarded-For`.
2. **uvicorn** reads it — but only from senders it trusts — and puts it in `request.client.host`. That's the
   `--proxy-headers --forwarded-allow-ips "*"` in the Dockerfile. Our middleware is unchanged.

Trust is the dangerous part: `X-Forwarded-For` is just a header, and **any client can send one**. We tried
three setups (requests from this PC, whose address on Docker's network is `172.19.0.1`; nginx is `172.19.0.6`):

| Setup | Key stored in Redis | What it means |
|---|---|---|
| A. uvicorn ignores the header | `ip:172.19.0.6` (nginx) | **Every** client shares one bucket |
| B. nginx **appends** to the client's header (`$proxy_add_x_forwarded_for`) | `ip:1.1.1.1`, `ip:2.2.2.2`, `ip:3.3.3.3` | A client sending fake IPs gets a **fresh bucket each time** — unlimited |
| C. nginx **overwrites** it (`$remote_addr`) — RateGate | `ip:172.19.0.1` only, even with fake headers | Correct |

So: nginx **overwrites** the header with the address it really saw, and uvicorn trusts the header. Trusting
it from any address (`"*"`) is safe here only because the API port is **never published**: nothing but nginx
can reach the copies. If the copies were reachable directly, you'd list nginx's address instead.

### 7. `X-Instance-Id`

To *see* the load balancing, every response gets a header naming the copy that answered. Its value is a new
setting, `instance_id`, which defaults to the machine's **hostname**. Inside Docker, a container's hostname is
its short container ID (like `517428e7f289`), so each copy names itself with no configuration.

It's added by a tiny second middleware registered **after** `RateLimitMiddleware`. In Starlette/FastAPI, the
middleware added last runs **first** and wraps everything inside it — so it also labels the `429` and `503`
answers that the rate-limit middleware returns without ever reaching a route.

### 8. When one copy dies

A big promise of horizontal scaling: one copy failing doesn't take the service down. nginx handles it with
**passive health checks**: if connecting to a copy fails, nginx tries the next one for that request, and skips
the failed copy for 10 seconds before trying it again.

**The default wait is far too long.** With nginx's defaults, the first request after we stopped a copy took
**38.8 seconds**: nginx kept trying to connect to the dead copy until the operating system gave up
(`Host is unreachable`). A stopped container doesn't refuse connections — its address simply vanishes, so
nothing answers at all. Copies on the same machine connect in milliseconds, so we set
`proxy_connect_timeout 1s`: after 1 s, nginx gives up on that copy and tries the next. Measured over 5 trials
of stopping a copy while sending 10 requests a second: **250 of 250 answered `200`, the slowest in 1.01 s.**

**nginx learns the addresses once, at startup.** After changing the number of copies
(`docker compose up -d --scale api=5`), run `docker compose restart nginx` so it looks them up again.
(nginx 1.27 can re-check DNS by itself with a `resolve` option. We tried it: it found new copies automatically,
but in 2 of 3 trials of stopping a copy, one request failed with `502` instead of being retried. Reliability
wins; we restart nginx after scaling instead.)

### 9. Two problems in earlier phases, found while preparing this one

The new tests and scripts put more load on Redis than before, and that exposed two problems. Both are fixed
first (Steps 1–2), so everything after builds on solid ground.

**(a) A retry could count one request twice (Phase 4 bug).** Running the concurrency tests repeatedly, one
run failed with:

```
assert 88 == 100
```

Only 88 of 200 requests were allowed when exactly 100 should have been. The cause is the one retry added in
Phase 4. redis-py's `Retry` retries on `ConnectionError` **and** `TimeoutError` by default. But a read timeout
means "the reply was late" — the Lua script **may already have run** in Redis. Retrying sends it again, so one
request is counted twice. We confirmed it by making the script's reply "late" on purpose (20 requests):

| Retry setting | Entries stored in Redis for 20 requests |
|---|---|
| Phase 4: retry on `ConnectionError` and `TimeoutError` | **40** — every request counted twice |
| Fixed: retry on `ConnectionError` only | **20** |

A connection error means the command never reached Redis (the connection was already dead — exactly the
stale-connection case the retry exists for), so retrying it is safe. A timeout is not. The error was in the
"strict" direction (clients blocked a little early, never let through extra), but it was still wrong.

**A bonus:** timeouts are no longer retried, so outages are noticed faster. Measured with the same `docker stop` / `docker pause` checks as Phase 4:

| Redis problem | Phase 4 | Now |
|---|---|---|
| Stopped (Windows dev server) | 1–2 s per request | **1.0 s** |
| Hung (`docker pause`) | about 0.5 s | **about 0.25 s** |
| Stopped, inside the Compose stack (Linux) | not measured | **1.0 s** |
| First request after Redis restarts | 0.01 s, allowed | 0.01–0.02 s, allowed |

Phase 4 predicted that on Linux a stopped Redis would be noticed much faster. Inside Docker it isn't: a stopped
container doesn't **refuse** connections — its address vanishes, so nothing answers at all, and the 1 s
connect timeout ends the wait, just as on Windows. (Measured; the prediction was wrong.)

**(b) A flaky Phase 3 test.** `test_limit_holds_across_separate_instances` failed in 5 of 10 runs today with
`Timeout connecting to server`, although it passed 10 of 10 in Phase 4. Even your unchanged project code
failed it sometimes. Measuring why:

| 200 new Redis connections opened at the same moment | Slowest |
|---|---|
| From Windows, through Docker Desktop's port forwarding (this morning, Phase 4) | about 600 ms |
| From Windows, through Docker Desktop's port forwarding (this afternoon) | **up to 2.5 s** |
| From inside Docker's own network (where the Compose stack runs) | up to 0.84 s |

The test makes four clients open up to 200 brand-new connections in the same instant, from one Windows
process, through Docker Desktop's port forwarding — which got slow after Docker Desktop restarted. Some
connections took longer than the 1 s connect timeout. A real server doesn't do that: its pool is opened as
traffic grows and then reused. The test exists to prove **atomicity**, not connection speed, so it now opens
each client's connections first ("warms up" the pool), then fires the burst. The production timeouts stay
as they are: inside Docker's network, where the real copies talk to Redis, replies during a 200-request burst
took at most 41 ms. 

In the scratch copy, after both fixes, 39 of 40 runs passed. **But when Phase 5 was then built in the project,
an hour later, the concurrency tests failed in 7 of 10 runs** — with the stack stopped as well as running —
now with `Timeout reading from localhost:6379`. Measuring the same 200-request burst 30 times over (6,000
replies), with no timeout cutting anything off:

| Where the test runs | Replies over 250 ms | Slowest reply |
|---|---|---|
| Windows → Docker Desktop's port forwarding (two rounds) | 0, then **50** | **269 ms** |
| Inside Docker's network (where the real copies run) | 0 | 47 ms |

The Windows → Docker path varies through the day, and at its worst it's slower than the 0.25 s reply timeout.
The production stack never takes that path, so its defaults stay. But Phase 4 made the timeouts **settings**
precisely because the right value depends on where Redis runs — and "tests on a Windows laptop, through
Docker Desktop" is a different place. So this machine gets its own values, in a `.env` file (Part B, step 4):

```
RATEGATE_REDIS_SOCKET_TIMEOUT=1.0
RATEGATE_REDIS_CONNECT_TIMEOUT=1.5
```

- **1.0 s for replies** — almost 4× the slowest measured (269 ms).
- **1.5 s to connect** — 3× the slowest of 4,000 measured connects (458 ms), and still **under 2 s**: Windows
  takes about 2 s to report a refused connection, and a refusal is retried (it's a connection error), so a
  connect timeout above 2 s would make the outage test wait for two refusals.
- **Only this machine sees it.** The app reads `.env` from the project folder (`env_file=".env"`, Phase 1), so
  `uv run pytest` and the dev server use it. The image doesn't contain it (`.dockerignore`), Compose doesn't pass
  those variables, and `.gitignore` keeps it out of git — so Phase 6's CI tests the real 0.25 s / 1 s. We
  checked inside a container: `0.25 1.0`.

With it: concurrency tests **30 of 30**, full suite 3 of 3.

---

## Part B — Setup

1. **Docker Desktop must be running** (whale icon in the taskbar, "Engine running"). If `docker ps` says
   *"failed to connect to the docker API"*, start Docker Desktop and wait about a minute.
2. **Your Phase 0 Redis must be running** for the tests: `docker start redis`, then
   `docker exec redis redis-cli ping` → `PONG`.
3. **Port 8080 must be free** (nginx will use it): `curl.exe -s http://127.0.0.1:8080` should print nothing
   and fail. If something answers, stop it, or change `"8080:80"` in `docker-compose.yml` to another port.
4. **Create `.env`** in the project folder (Concept 9b) — settings for this machine only:

   ```
   # Settings for this machine only (never committed).
   # Windows -> Docker Desktop adds delays under bursts (replies up to ~270 ms,
   # connections up to ~460 ms measured), so the production defaults (0.25 s, 1 s)
   # are too tight for running the tests from here. Keep the connect timeout under
   # 2 s: Windows takes about 2 s to report a refused connection.
   RATEGATE_REDIS_SOCKET_TIMEOUT=1.0
   RATEGATE_REDIS_CONNECT_TIMEOUT=1.5
   ```

   and add it to `.gitignore`, so it's never committed:

   ```
   # Per-machine settings (may hold secrets; never committed)
   .env
   ```

   Check it's picked up: `uv run python -c "from app.config import settings; print(settings.redis_socket_timeout)"`
   → `1.0`. (VS Code may then create `.vscode/settings.json` to load `.env` in its terminals too; that's your
   editor's file — leave it out of commits.)

Nothing to install: the base images (`python:3.12-slim`, `nginx:1.27-alpine`, `redis:7` and uv's image) are
downloaded automatically the first time you build.

---

## Part C — Build it (in this order)

Files touched (★ new, ✎ changed):

```
app/
  redis_client.py              ✎ Step 1 (retry only connection errors)
  config.py                    ✎ Step 3 (instance_id setting)
  main.py                      ✎ Step 3 (X-Instance-Id middleware)
tests/
  test_fail_modes.py           ✎ Step 1 (late-reply test, tighter outage budget)
  test_concurrency.py          ✎ Step 2 (warm up the pools)
  test_instance_id.py          ★ Step 3
Dockerfile                     ★ Step 4
.dockerignore                  ★ Step 4
nginx/nginx.conf               ★ Step 5
docker-compose.yml             ★ Step 6
scripts/sequential_requests.py ★ Step 8
scripts/concurrent_burst.py    ✎ Step 8 (answers per copy)
README.md, ROADMAP.md          ✎ Step 10
.gitignore                     ✎ Part B (.env stays out of git)
```

| Step | What | Why here |
|---|---|---|
| 1 | Fix the double-counting retry (test first) | A correctness bug; everything after relies on exact counts |
| 2 | Warm the pools in the 4-client test | A trustworthy test suite before adding to it |
| 3 | `X-Instance-Id`: test → setting → middleware | The one app change Phase 5 needs; testable without Docker |
| 4 | `Dockerfile` + `.dockerignore`, build, run **one** container | Package the app; check it alone before multiplying it |
| 5 | `nginx/nginx.conf` | The load balancer's rules |
| 6 | `docker-compose.yml` | Describe the whole system |
| 7 | `docker compose up` | Start it and check every piece is healthy |
| 8 | The two scripts | Make the load balancing and the shared limit visible |
| 9 | Run everything | Prove nothing older broke |
| 10 | README and ROADMAP | Record it |

### Step 1 — Fix the retry, test first

**1a. The test.** In `tests/test_fail_modes.py`, add to the imports:

```python
import asyncio
import time
...
from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import Settings, settings
from app.limiter.sliding_log import SlidingWindowLog
from app.limiter.token_bucket import TokenBucket
```

and at the end of the file:

```python
async def test_a_late_reply_is_counted_once(redis, client_id, monkeypatch):
    limiter = SlidingWindowLog(redis, limit=10, window_seconds=60)
    await limiter.check(f"{client_id}-warm")  # makes sure Redis has the script loaded
    original = Redis.parse_response

    async def late_reply(self, connection, command_name, **options):
        # The script has been sent and Redis runs it; only its answer is "late".
        # Do what redis-py does on a real read timeout: drop the connection, then raise.
        if command_name == "EVALSHA":
            await asyncio.sleep(0.05)
            await connection.disconnect(nowait=True)
            raise RedisTimeoutError("Timeout reading from Redis (simulated)")
        return await original(self, connection, command_name, **options)

    monkeypatch.setattr(Redis, "parse_response", late_reply)
    with pytest.raises(RedisError):
        await limiter.check(client_id)
    monkeypatch.undo()
    # One request, one entry. A retry after the timeout would have run it twice.
    assert await redis.zcard(f"rategate:swl:{client_id}") == 1
```

**What's happening:**

- **`monkeypatch`** is a built-in pytest fixture that temporarily replaces something — here, redis-py's
  `Redis.parse_response`, the method that reads a command's reply — and puts the original back afterwards
  (`monkeypatch.undo()`, and automatically at the end of the test).
- **`late_reply`** lets every command work normally, except the limiter's script (`EVALSHA` is how redis-py
  runs a stored Lua script). For that one, the command has **already been sent** — Redis runs it within a
  millisecond — and then we pretend the reply is late: wait 50 ms, then do exactly what redis-py does on a real
  read timeout — close the connection and raise `TimeoutError`. That's the situation that double-counted.
- **Why close the connection?** Our first version of this test only raised the error. The unread reply then
  stayed on the connection, and the *next* command (`zcard`) received it — the test printed
  `assert [1, 9, 0, 60000] == 1`, the script's reply. Reading redis-py's source showed that a real timeout
  disconnects first, so the test now does too. A simulation must copy the real behaviour, or it tests itself.
- **The warm-up call** makes sure Redis already has the script stored. Otherwise the first `EVALSHA` would be
  refused ("no such script") and nothing would be counted — the test would fail for the wrong reason (Phase 4's lesson).
- **`pytest.raises(RedisError)`**: the check must still fail — the client doesn't know the result.
- **`zcard`** counts the entries in the sliding log's sorted set: one request must mean one entry.

**1b. Tighten the outage-speed test.** In the same file, `test_outage_is_detected_within_the_timeouts`
becomes:

```python
async def test_outage_is_detected_within_the_timeouts(redis_down, client_id):
    # A connect timeout is not retried, so the worst case is one connect timeout.
    budget = settings.redis_connect_timeout + 0.5
    start = time.perf_counter()
    await call_api(TokenBucket(redis_down, 10, 1.0), "open", client_id)
    assert time.perf_counter() - start < budget
```

With timeouts no longer retried, a dead Redis costs one connect timeout, not two. The budget says so.

**Run them on the current code — both must fail:**

```powershell
uv run pytest tests/test_fail_modes.py -v
```

Output on the current code:

```
test_fail_open_allows_requests_when_redis_is_down PASSED
test_fail_closed_rejects_requests_when_redis_is_down PASSED
test_outage_is_detected_within_the_timeouts FAILED
test_limiting_resumes_after_an_outage PASSED
test_fail_mode_accepts_only_open_or_closed PASSED
test_a_late_reply_is_counted_once FAILED

E       assert (184177.1529925 - 184175.1280744) < 1.5
E       assert 2 == 1
```

- **`< 1.5` failed after 2.02 s:** the connect timed out, was retried, and timed out again.
- **`2 == 1`:** one request, two entries in Redis — the double counting, caught by a test.

**1c. The fix,** in `app/redis_client.py`: one import and one changed line.

```python
from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

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
        # Only connection errors are retried: after a timeout the script may already
        # have run, and running it again would count the request twice.
        retry=Retry(NoBackoff(), retries=1, supported_errors=(RedisConnectionError,)),
        decode_responses=True,
    )
    return Redis.from_pool(pool)
```

- **`supported_errors=(RedisConnectionError,)`** — the errors this retry policy reacts to. Any other error
  (including `TimeoutError`) is raised straight away. The trailing comma makes it a one-item tuple.
- **`as RedisConnectionError`** — Python has its own built-in `ConnectionError`. Importing redis-py's under a
  different name avoids hiding Python's and makes clear which one is meant.
- **Still handles a Redis restart:** we restarted Redis under a full pool, and the first request afterwards was
  allowed in 0.009 s — the stale connection raised `ConnectionError`, which is still retried.

Run again: both pass.

### Step 2 — Warm the pools in the 4-client test

In `tests/test_concurrency.py`, add a helper above the first test:

```python
async def warm_up(client: Redis) -> None:
    # Open the pool's connections before the burst, as a running server's pool already
    # is. The test is about atomicity, not about opening 200 connections in one instant.
    await asyncio.gather(
        *(client.ping() for _ in range(settings.redis_max_connections))
    )
```

and call it in `test_limit_holds_across_separate_instances`, inside the `try`, before the burst:

```python
    try:
        for client in clients:
            await warm_up(client)
        decisions = await asyncio.gather(
            *(limiters[i % 4].check(client_id) for i in range(ATTEMPTS))
        )
```

- **50 simultaneous `PING`s** make the pool open its 50 connections (each ping needs one). Doing it client by
  client means at most 50 connections open at once, which is fast.
- The burst then reuses open connections, like a server that has been running for a while. The test still
  proves the same thing: four independent clients, one limit.
- The warm-up sits **inside** `try`, so the clients are closed even if it fails.

### Step 3 — `X-Instance-Id`: test, setting, middleware

**3a. `tests/test_instance_id.py` (new):**

```python
import socket

import httpx

from app.config import Settings, settings
from app.limiter.token_bucket import TokenBucket
from app.main import app


async def get(path, limiter, api_key, times=1):
    """Send requests through the real app, the way nginx would hand them over."""
    app.state.limiter = limiter
    app.state.fail_mode = "open"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-API-Key": api_key}
        return [await client.get(path, headers=headers) for _ in range(times)]


async def test_allowed_and_blocked_responses_name_the_instance(redis, client_id):
    limiter = TokenBucket(redis, capacity=1, refill_rate=0.001)
    responses = await get("/api/resource", limiter, client_id, times=2)
    assert [response.status_code for response in responses] == [200, 429]
    for response in responses:
        assert response.headers["x-instance-id"] == settings.instance_id


async def test_health_check_names_the_instance(redis, client_id):
    [response] = await get("/health", TokenBucket(redis, 10, 1.0), client_id)
    assert response.headers["x-instance-id"] == settings.instance_id


def test_instance_id_defaults_to_the_hostname():
    assert Settings().instance_id == socket.gethostname()
    assert Settings(instance_id="api-7").instance_id == "api-7"
```

- **Test 1** — a bucket of 1 gives `[200, 429]`; **both** must carry the header. The `429` is the important
  one: it's made by the rate-limit middleware without reaching the route, so it proves the new middleware
  wraps it (Concept 7).
- **Test 2** — `/health` is exempt from limiting, but still labelled. Useful for checking the load balancer.
- **Test 3** — the default is the hostname, and the setting can be overridden. `socket.gethostname()` is
  Python's way to ask the operating system for the machine's name.

Run it on the current code: **3 failed** — `KeyError: 'x-instance-id'` twice (no such header) and
`AttributeError: 'Settings' object has no attribute 'instance_id'`.

**3b. `app/config.py`** — two imports and one setting:

```python
import socket
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
```

```python
    fail_mode: FailMode = "open"  # what to do when Redis can't be reached
    instance_id: str = Field(default_factory=socket.gethostname)  # names this copy
```

- **`Field(default_factory=...)`** — a default that is **computed** (by calling the function) when settings
  are created, instead of a fixed value. Each container computes its own hostname.
- As with every setting, `RATEGATE_INSTANCE_ID` can override it.

**3c. `app/main.py`** — after `app.add_middleware(RateLimitMiddleware)`:

```python
@app.middleware("http")
async def add_instance_id(request, call_next):
    # Added last, so it wraps everything: 200s, 429s and 503s all say who answered.
    response = await call_next(request)
    response.headers["X-Instance-Id"] = settings.instance_id
    return response
```

- **`@app.middleware("http")`** — FastAPI's short way to write a middleware as one function, without a class.
- **`call_next(request)`** passes the request inward (to the rate-limit middleware, then the route) and
  returns the response; we add one header and return it.

Run the tests: **3 passed**.

### Step 4 — `Dockerfile` and `.dockerignore`, then one container

**`Dockerfile`** (project root, no extension):

```dockerfile
# The Python version matches .python-version.
FROM python:3.12-slim

# uv, copied from its official image (pinned to the version used to write uv.lock).
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

WORKDIR /app

# Use the image's Python instead of downloading one; copy packages into .venv
# rather than linking them, and precompile them so the first request is fast.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Dependencies first: this layer is rebuilt only when the lock file changes,
# not on every code change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY app ./app

# Run the virtual environment's programs directly, with no uv at runtime.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# --proxy-headers: take the client's IP from nginx's X-Forwarded-For header.
# --forwarded-allow-ips "*": trust that header from any address, which is safe
# only because the API port is never published; nginx is the only way in.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
```

**Line by line:**

- **`FROM python:3.12-slim`** — Python 3.12, the same version `.python-version` pins locally. `slim` is a
  smaller variant without compilers and extras we don't need.
- **`COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv`** — copies just the `uv` program out of uv's
  official image. Pinning the exact version (the one you have locally) means the image is built the same way
  every time.
- **`UV_PYTHON_DOWNLOADS=never`** — use the image's Python 3.12, never download another.
- **`UV_LINK_MODE=copy`** — copy package files into `.venv` (linking doesn't work across Docker's layers, and
  uv would print a warning).
- **`UV_COMPILE_BYTECODE=1`** — turn the libraries' `.py` files into `.pyc` during the build, so the first
  request after start isn't slowed down by compiling.
- **`COPY pyproject.toml uv.lock .python-version ./`** then **`RUN uv sync --frozen --no-dev`** — install
  exactly the versions in `uv.lock` (`--frozen`: never change the lock file; fail if it's out of date) and skip
  the dev tools (`--no-dev`: no pytest or ruff in production). Because only these three files are copied first,
  this slow step is cached until your dependencies change (Concept 2).
- **`COPY app ./app`** — now the code. Tests, scripts and docs stay out: the image only needs to run the API.
- **`ENV PATH="/app/.venv/bin:$PATH"`** — put the virtual environment's programs first on the search path, so
  `uvicorn` means the `.venv` one. No `uv run` needed when the container starts.
- **`--host 0.0.0.0`** — listen on every network interface of the container. The default, `127.0.0.1`, would
  only accept connections from **inside** the container itself, and nginx couldn't reach it.
- **`--proxy-headers --forwarded-allow-ips "*"`** — Concept 6.
- **The `CMD [...]` list form** runs uvicorn directly (no shell in between), so when Docker stops the
  container, uvicorn receives the stop signal and shuts down cleanly.

**`.dockerignore`:**

```
# Only what the Dockerfile copies is needed; everything else would slow the
# build and could leak local files into the image.
.git
.venv
.env
**/__pycache__
.pytest_cache
.ruff_cache
docs
tests
scripts
```

`.venv` matters most: it holds **Windows** programs that can't run in a Linux container, and it's large.
`.env` could contain secrets.

**Build the image and try one container** before multiplying it:

```powershell
docker build -t rategate-api .
docker run --rm -d --name rategate-single -p 8001:8000 -e RATEGATE_REDIS_URL=redis://host.docker.internal:6379/0 rategate-api
curl.exe -s -i http://127.0.0.1:8001/api/resource
docker stop rategate-single
```

Output:

```
HTTP/1.1 200 OK
server: uvicorn
content-type: application/json
x-ratelimit-limit: 10
x-ratelimit-remaining: 9
x-ratelimit-reset: 1
x-instance-id: 7cc24e88f33b

{"message":"You got through!"}
```

`x-instance-id` is the container's own ID (`docker ps` shows the same `7cc24e88f33b`). The packaged app works
on its own — now we can run three.

- **`docker build -t rategate-api .`** — build from the `Dockerfile` in `.` (this folder) and name (**t**ag) the
  image `rategate-api`.
- **`docker run --rm -d --name rategate-single -p 8001:8000 -e ... rategate-api`** — start one container:
  `--rm` deletes it when it stops, `-d` runs it in the background, `-p 8001:8000` publishes container port 8000
  as your PC's 8001, `-e` sets an environment variable inside it.
- **`host.docker.internal`** — a special name Docker Desktop provides: "the PC this container runs on". It lets
  this one container reach your Phase 0 Redis on `localhost:6379`.

### Step 5 — `nginx/nginx.conf`

Create the folder `nginx` and in it `nginx.conf`:

```nginx
upstream rategate_api {
    # Shared memory for the round-robin count. Without it, each nginx worker process
    # keeps its own count, all starting at the first server, so one replica gets
    # most requests.
    zone rategate_api 64k;
    # "api" is the Compose service name. When nginx starts, Docker's DNS answers it
    # with the address of every replica, so this one line becomes one server per
    # replica, used in turn. After changing the number of replicas, restart nginx.
    server api:8000;
}

server {
    listen 80;

    location / {
        proxy_pass http://rategate_api;
        # Replicas are on the same machine and answer in milliseconds. If one is dead,
        # give up on it after 1 s and try the next, instead of waiting the 60 s default.
        proxy_connect_timeout 1s;
        proxy_set_header Host $host;
        # Overwrite, never append: a client can't smuggle in a fake IP of its own.
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

- **`upstream rategate_api { ... }`** — a named group of backend servers (Concept 4).
- **`zone rategate_api 64k;`** — 64 kilobytes of shared memory for the group's state, so all worker processes
  share one round-robin count. Without it, one copy got all the traffic.
- **`server api:8000;`** — the copies: Compose's DNS name for the service, port 8000.
- **`server { listen 80; ... }`** — the website nginx serves, on port 80 inside its container (published as 8080).
- **`location / { ... }`** — rules for every path starting with `/`, i.e. all of them.
- **`proxy_pass http://rategate_api;`** — forward the request to the upstream group.
- **`proxy_connect_timeout 1s;`** — Concept 8.
- **`proxy_set_header Host $host;`** — keep the original `Host` header (by default nginx would send the group's
  name). `$host` is one of nginx's built-in variables.
- **`proxy_set_header X-Forwarded-For $remote_addr;`** — `$remote_addr` is the address of whoever connected to
  nginx. Setting the header to it **replaces** anything the client sent (Concept 6).

nginx's own syntax: one setting per line, each ending in `;`, blocks in `{ }`, comments with `#`.

### Step 6 — `docker-compose.yml`

```yaml
name: rategate

services:
  redis:
    image: redis:7
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      retries: 10

  api:
    build: .
    environment:
      RATEGATE_REDIS_URL: redis://redis:6379/0
      # Passed through from your terminal when set there; otherwise the app's defaults.
      RATEGATE_ALGORITHM:
      RATEGATE_CAPACITY:
      RATEGATE_REFILL_RATE:
      RATEGATE_LIMIT:
      RATEGATE_WINDOW_SECONDS:
      RATEGATE_FAIL_MODE:
    deploy:
      replicas: 3
    depends_on:
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 2s
      retries: 15

  nginx:
    image: nginx:1.27-alpine
    ports:
      - "8080:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      api:
        condition: service_healthy
```

**Piece by piece.** YAML is indentation-based (spaces, never tabs): what's indented under a name belongs to it.

- **`name: rategate`** — the project name. Container names start with it: `rategate-api-1`, `rategate-nginx-1`.
- **`redis`** — the official Redis 7 image. The **health check** runs `redis-cli ping` every 2 s; after 10
  failures it's marked unhealthy. No `ports:` — Redis is private to the stack.
- **`api` → `build: .`** — build the image from our `Dockerfile` instead of downloading one.
- **`environment:`** — `RATEGATE_REDIS_URL` points at the service named `redis` (Docker's DNS, Concept 3). The
  names with no value are passed through from your terminal when set there (Concept 3).
- **`deploy: replicas: 3`** — three containers from this service.
- **`depends_on: redis: condition: service_healthy`** — start the API only once Redis answers `PONG`.
- **API health check** — runs a one-line Python program inside the container: fetch `/health`; if the request
  fails, Python exits with an error and Docker counts a failure. (`python:3.12-slim` has no `curl`, but it has
  Python.)
- **`nginx` → `image: nginx:1.27-alpine`** — official nginx, version 1.27, on the tiny Alpine Linux. A fixed
  version means the same nginx every time.
- **`ports: - "8080:80"`** — your PC's port 8080 → nginx's port 80. The **only** way into the stack.
- **`volumes: - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro`** — show our config file inside the
  container, where nginx looks for site configuration, replacing its default site. `:ro` = read-only.
- **`depends_on: api: condition: service_healthy`** — nginx starts once the copies are healthy, so its DNS
  lookup of `api` finds all three.

### Step 7 — Start the stack

```powershell
docker compose up --build -d --wait
docker compose ps
```

- **`up`** — create and start everything in the file. **`--build`** — build the API image first (needed after
  code changes). **`-d`** — in the background ("detached"), giving you the terminal back. **`--wait`** — return
  only when every service is running and healthy.

Expected (`docker compose ps`):

```
NAME               IMAGE               STATUS                    PORTS
rategate-api-1     rategate-api        Up 6 seconds (healthy)    8000/tcp
rategate-api-2     rategate-api        Up 5 seconds (healthy)    8000/tcp
rategate-api-3     rategate-api        Up 6 seconds (healthy)    8000/tcp
rategate-nginx-1   nginx:1.27-alpine   Up 1 second               0.0.0.0:8080->80/tcp, [::]:8080->80/tcp
rategate-redis-1   redis:7             Up 26 seconds (healthy)   6379/tcp
```

Only nginx shows `0.0.0.0:8080->80/tcp` — published. The others list their port without an arrow — private.
Then one request:

```powershell
curl.exe -s -i http://127.0.0.1:8080/api/resource
```

```
HTTP/1.1 200 OK
Server: nginx/1.27.5
...
x-ratelimit-limit: 10
x-ratelimit-remaining: 9
x-ratelimit-reset: 1
x-instance-id: c9e26d8f1474

{"message":"You got through!"}
```

`Server: nginx` shows nginx answered you; `x-instance-id` shows which copy did the work behind it.

### Step 8 — The two scripts

**`scripts/sequential_requests.py` (new)** — one request at a time, printing who answered:

```python
"""Send requests one at a time and show which server copy answered each, and how."""

import argparse
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/resource")
    parser.add_argument("--total", type=int, default=14)
    parser.add_argument("--api-key", default=f"seq-{uuid4().hex[:8]}")
    args = parser.parse_args()

    print(f"API key {args.api_key}")
    with httpx.Client(timeout=10) as client:
        for n in range(1, args.total + 1):
            response = client.get(args.url, headers={"X-API-Key": args.api_key})
            instance = response.headers.get("X-Instance-Id", "(no header)")
            remaining = response.headers.get("X-RateLimit-Remaining", "-")
            print(f"{n:3}  {response.status_code}  {instance}  remaining={remaining}")


if __name__ == "__main__":
    main()
```

- A **synchronous** `httpx.Client` (no `async`): one request, wait for the answer, then the next.
- A fresh random API key per run (`uuid4`), so each run starts with a full bucket.
- `response.headers.get(name, default)` returns the default instead of crashing when a header is missing (e.g.
  `X-RateLimit-Remaining` on a fail-open response).
- `{n:3}` pads the number to 3 characters so the columns line up.

**`scripts/concurrent_burst.py` (changed)** — `burst` now returns the responses themselves, and `main` adds a
per-copy summary:

```python
from collections import Counter, defaultdict
...
async def burst(url: str, total: int, api_key: str) -> list[httpx.Response]:
    limits = httpx.Limits(max_connections=total)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        return await asyncio.gather(
            *(client.get(url, headers={"X-API-Key": api_key}) for _ in range(total))
        )
...
    responses = asyncio.run(burst(args.url, args.total, args.api_key))
    counts = Counter(response.status_code for response in responses)
    print(f"{args.total} requests at once, API key {args.api_key}:")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    # Which server copy answered, and how: only meaningful behind a load balancer.
    by_instance = defaultdict(Counter)
    for response in responses:
        instance = response.headers.get("X-Instance-Id", "(no header)")
        by_instance[instance][response.status_code] += 1
    print("By instance:")
    for instance, statuses in sorted(by_instance.items()):
        summary = ", ".join(f"{s}: {n}" for s, n in sorted(statuses.items()))
        print(f"  {instance}  {summary}")
```

- **`defaultdict(Counter)`** — a dictionary that creates an empty `Counter` the first time a new key is used,
  so `by_instance[instance][status] += 1` works without checking first.
- **`", ".join(...)`** — glue strings together with `", "` between them: `200: 34, 429: 33`.

### Step 9 — Run everything

```powershell
foreach ($i in 1..3) { uv run pytest -q | Select-Object -Last 1 }
foreach ($i in 1..10) { uv run pytest -q tests/test_concurrency.py | Select-Object -Last 1 }
uv run ruff check .
uv run ruff format --check .
```

Expected:

```
40 passed in 26.93s
40 passed in 26.81s
40 passed in 26.61s
```

`7 passed` ten times, then `All checks passed!` and `20 files already formatted`. 40 = Phase 4's 36 + 3
instance-id tests + the late-reply test. The suite is a little **faster** than Phase 4's 29 s: outage tests now
wait one connect timeout (1.5 s with this machine's `.env`) instead of two of 1 s each.

If a concurrency test fails with `Timeout reading` or `Timeout connecting`, check `.env` exists and is read
(Part B, step 4).

### Step 10 — README and ROADMAP

Add a **Run the full stack** section to the README:

````markdown
## Run the full stack (3 copies behind nginx)

```bash
docker compose up --build -d --wait
```

This starts Redis, three copies of the API and nginx, which spreads requests across the copies in turn. Call
`http://127.0.0.1:8080/api/resource`: the `X-Instance-Id` header names the copy that answered, while every
copy checks the same Redis, so a client is limited once, globally.
````

…plus the commands for the two scripts and `docker compose down`, the `RATEGATE_INSTANCE_ID` setting, the new
test count, and the corrected failure-handling numbers. Tick Phase 5 in `ROADMAP.md`.

---

## Part D — Verify (checks, with real outputs)

Run these with the stack up (`docker compose up --build -d --wait`), from the project folder.

### Check 1 — The copies take turns, and share one count

```powershell
uv run python scripts/sequential_requests.py
```

```
API key seq-d3b9cb4d
  1  200  517428e7f289  remaining=9
  2  200  400d48e5fba2  remaining=8
  3  200  c9e26d8f1474  remaining=7
  4  200  517428e7f289  remaining=6
  5  200  400d48e5fba2  remaining=5
  6  200  c9e26d8f1474  remaining=4
  7  200  517428e7f289  remaining=3
  8  200  400d48e5fba2  remaining=2
  9  200  c9e26d8f1474  remaining=1
 10  200  517428e7f289  remaining=0
 11  429  400d48e5fba2  remaining=0
 12  429  c9e26d8f1474  remaining=0
 13  429  517428e7f289  remaining=0
 14  429  400d48e5fba2  remaining=0
```

**Meaning:** three different IDs, in a repeating order — round-robin works. `remaining` falls 9, 8, 7 … across
**different** copies — they share one bucket. Request 11 is refused by a copy that had served only 3 requests
itself: it refuses because of what the **other** copies did. (Your IDs will differ: they're container IDs.)
Sometimes request 11 still gets `200`: the bucket refills 1 token per second, and the run takes about a second.

### Check 2 — A burst of 200, through all three copies

Default limit (token bucket, capacity 10):

```powershell
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200
```

```
200 requests at once, API key burst-5bcc17ed:
  200: 10
  429: 190
By instance:
  400d48e5fba2  200: 4, 429: 62
  517428e7f289  200: 3, 429: 64
  c9e26d8f1474  200: 3, 429: 64
```

Then Phase 3's exact test — limit 100, nothing refilling — through the whole stack, for each algorithm:

```powershell
$env:RATEGATE_CAPACITY = "100"; $env:RATEGATE_REFILL_RATE = "0.001"
docker compose up -d --wait
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200
$env:RATEGATE_ALGORITHM = "sliding_log"; $env:RATEGATE_LIMIT = "100"; $env:RATEGATE_WINDOW_SECONDS = "60"
docker compose up -d --wait
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200
$env:RATEGATE_ALGORITHM = "sliding_counter"
docker compose up -d --wait
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200
Remove-Item Env:RATEGATE_*
docker compose up -d --wait
docker compose restart nginx
```

`docker compose up -d` again **recreates** the API copies whose settings changed (it prints `Recreated`).
New copies may get new addresses, so nginx is restarted at the end (Concept 8).

| Algorithm | Run | Allowed | Per copy |
|---|---|---|---|
| Token bucket | 1 | **100** | 34, 33, 33 |
| Token bucket | 2 | **100** | 39, 30, 31 |
| Token bucket | 3 | **100** | 34, 32, 34 |
| Sliding window log | 1 | **100** | 32, 33, 35 |
| Sliding window counter | 1 | **100** | 33, 31, 36 |

**Meaning:** exactly 100 every time, while no copy allowed more than 39. Each copy alone would have allowed
100; together they allowed 100, not 300 (Concept 5).

### Check 3 — The real client IP, and no spoofing

```powershell
docker compose exec redis redis-cli flushall
curl.exe -s -o NUL http://127.0.0.1:8080/api/resource
curl.exe -s -o NUL -H "X-Forwarded-For: 1.2.3.4" http://127.0.0.1:8080/api/resource
docker compose exec redis redis-cli --scan --pattern "rategate:*"
```

```
rategate:tb:ip:172.19.0.1
```

- **`docker compose exec redis redis-cli ...`** — run `redis-cli` inside the stack's Redis container (not your
  Phase 0 one). `flushall` empties it so only these requests' keys remain. Never run it on data you care about.
- **One key**, holding your PC's address on Docker's network (yours may differ, e.g. `172.18.0.1`) — not nginx's
  address, and not the fake `1.2.3.4`. Both requests used the same bucket.

### Check 4 — One copy dies

```powershell
docker stop rategate-api-2
foreach ($i in 1..10) { curl.exe -s -o NUL -w "%{http_code} in %{time_total}s`n" http://127.0.0.1:8080/health }
uv run python scripts/sequential_requests.py --total 6
docker start rategate-api-2
```

Expected: every answer `200`; at most one takes about 1 s (nginx giving up on the dead copy); only two IDs
answer until the copy is back. Our 5 trials, 50 requests each: **250 × `200`, slowest 1.01 s**. After
`docker start`, the copy gets traffic again within about 10 s (nginx's retry interval).

(`` `n `` inside the double-quoted PowerShell string is a newline, so each result prints on its own line.)

### Check 5 — Redis dies, inside the stack

```powershell
docker compose stop redis
foreach ($i in 1..3) { curl.exe -s -o NUL -w "%{http_code} in %{time_total}s`n" http://127.0.0.1:8080/api/resource }
docker compose start redis
curl.exe -s -o NUL -w "%{http_code} in %{time_total}s`n" http://127.0.0.1:8080/api/resource
```

Ours (fail-open, the default):

```
200 in 1.025917s
200 in 1.007891s
200 in 1.007874s
200 in 0.008788s
```

and with `$env:RATEGATE_FAIL_MODE = "closed"` before `docker compose up -d --wait` (then `docker compose restart nginx`):

```
503 in 1.007128s
503 in 1.008457s
503 in 1.007850s
200 in 0.009137s
```

`docker compose logs api` shows lines like `api-2  | Redis unavailable, rejecting request (fail-closed): Timeout
connecting to server` — the `api-2  |` prefix says which copy wrote it. Phase 4's behaviour holds in every copy.
Finish with `Remove-Item Env:RATEGATE_FAIL_MODE`, `docker compose up -d --wait` and `docker compose restart nginx`.

### Check 6 — Scale to 5 copies, and back

```powershell
docker compose up -d --scale api=5 --wait
docker compose restart nginx
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 50
docker compose up -d --scale api=3 --wait
docker compose restart nginx
```

Ours, with 5 copies:

```
50 requests at once, API key burst-017ba9f7:
  200: 10
  429: 40
By instance:
  0ea0f865c43b  200: 3, 429: 7
  1640f7389ac1  200: 2, 429: 8
  20ae0b2066c7  200: 3, 429: 7
  7a8c82fb07d4  200: 2, 429: 8
  d2627f036ad4  429: 10
```

Five IDs, 10 requests each — nginx used all five. Still exactly **10** allowed in total. (One copy happened to
get only `429`s: by the time its requests arrived, the bucket was empty.) Back at 3 copies: three IDs, 10 allowed.
This is horizontal scaling in one command: more copies, same limit, no code or config change except restarting nginx.

### Check 7 — Clean up

```powershell
docker compose down
docker compose ps
docker exec redis redis-cli ping
```

`docker compose down` stops **and removes** the stack's containers and network (the image stays, so the next
`up` is fast). `ps` then lists nothing. Your Phase 0 Redis is untouched: `PONG`.

---

## Part E — Common pitfalls

| Pitfall | Symptom | Why | Fix |
|---|---|---|---|
| Docker Desktop not running | `failed to connect to the docker API ... dockerDesktopLinuxEngine` | The Docker engine is off | Start Docker Desktop, wait for "Engine running" |
| No `zone` in the upstream | Every request answered by the same copy | Each nginx worker has its own round-robin count | `zone rategate_api 64k;` |
| Default `proxy_connect_timeout` | One request hangs ~40 s after a copy dies | nginx waits for the OS to give up on a vanished address | `proxy_connect_timeout 1s;` |
| Not restarting nginx after scaling | New copies get no traffic; or a removed copy is still tried | nginx looked up `api` only at startup | `docker compose restart nginx` |
| Trusting `X-Forwarded-For` without overwriting it | Clients bypass limits with fake IPs | The header is client-controlled | nginx: `X-Forwarded-For $remote_addr` |
| Not trusting it at all | Every client shares nginx's bucket | The API only sees nginx's address | uvicorn `--proxy-headers --forwarded-allow-ips` |
| Publishing the API copies' port | Clients can skip nginx and fake `X-Forwarded-For` | `"*"` trusts everyone who can connect | Publish only nginx |
| uvicorn on `127.0.0.1` in a container | nginx gets `502 Bad Gateway` | That address accepts connections from inside the container only | `--host 0.0.0.0` |
| Copying `.venv` into the image | Huge build context; broken Windows programs inside Linux | No `.dockerignore` | `.dockerignore` with `.venv` |
| Copying `app/` before installing dependencies | Every code change reinstalls all libraries | The cache breaks at the first changed layer | Lock files first, `uv sync`, then `app/` |
| `localhost` as the Redis address inside a container | API can't reach Redis | `localhost` is the container itself | The service name: `redis://redis:6379/0` |
| Forgetting `--build` after a code change | Your change doesn't appear | Compose reuses the old image | `docker compose up --build -d --wait` |
| `$env:RATEGATE_*` left set | Later `docker compose up` silently uses odd limits | Passed through from the terminal | `Remove-Item Env:RATEGATE_*` |
| Port 8080 already in use | `Bind for 0.0.0.0:8080 failed: port is already allocated` | Another program uses it | Stop it, or change `"8080:80"` |
| Retrying after a timeout | Requests counted twice | The script may have run before the reply was late | Retry connection errors only |
| Production timeouts on a Windows dev machine | Tests fail now and then with `Timeout reading` / `Timeout connecting` | Docker Desktop's port forwarding adds delays under bursts | Looser values in this machine's `.env` (Concept 9b) |
| Committing `.env` | Machine-specific values (later, secrets) end up in git and CI | It's just a file in the folder | `.env` in `.gitignore` |
| Running `flushall` on the wrong Redis | Your other data is gone | Two Redis servers exist | `docker compose exec redis ...` targets the stack's |

---

## Part F — Finish the phase

**Done when:** `docker compose up` → responses come from different instances, but the client is limited once,
globally (the roadmap's "done when"), and the full test suite passes repeatedly.

```powershell
docker compose down
git add .
git commit -m "Phase 5: 3 API replicas behind nginx sharing one Redis; fix retry double counting"
git push
```

Then tick Phase 5 in `ROADMAP.md`.

**Check your understanding:**

1. What's the difference between vertical and horizontal scaling, and why does horizontal scaling need a
   stateless app?
2. What's the difference between an image and a container?
3. Why does the Dockerfile copy `pyproject.toml` and `uv.lock` before `app/`?
4. How does `api:8000` in nginx's config become three servers?
5. Why did all requests go to one copy before `zone` was added?
6. Why would three copies allow 300 requests if each counted in its own memory, and what makes RateGate allow 100?
7. Why does the API need `--proxy-headers`, and why must nginx overwrite `X-Forwarded-For` instead of appending?
8. Why is `--forwarded-allow-ips "*"` safe here, and when wouldn't it be?
9. Why is the `X-Instance-Id` middleware added after the rate-limit middleware?
10. Why did a request take 38.8 s after a copy was stopped, and what fixed it?
11. Why must a retry after a timeout not resend the limiter's script?
12. Why do the containers reach Redis as `redis`, but the tests as `localhost`?
