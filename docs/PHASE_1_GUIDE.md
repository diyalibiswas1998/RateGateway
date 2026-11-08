# Phase 1 Guide — One API, One Algorithm (Token Bucket)

**Goal of this phase:** a FastAPI app where each client can make a limited number of requests.
When they go over the limit they get `HTTP 429 Too Many Requests`. The counting happens in Redis.

Type the code yourself instead of copy-pasting where you can. You'll remember far more.

---

## Part A — Concepts first (read before coding)

### 1. What is rate limiting?
It means capping how often a client can use your API, e.g. "10 requests, refilling at 1 per second".
It protects your server from overload, abuse, and runaway scripts.

### 2. The token bucket algorithm
Picture each client owning a bucket:

- The bucket holds at most **capacity** tokens (e.g. 10). It starts full.
- Every request **takes 1 token**. No token → request rejected (429).
- Tokens **drip back in** at a steady **refill rate** (e.g. 1 per second), never above capacity.

So a client can **burst** 10 requests instantly, then is limited to ~1/second afterwards.

**The trick:** we don't run a timer that adds tokens every second. Instead, on each request we compute:

```
elapsed   = now - last_time_we_looked
tokens    = min(capacity, tokens + elapsed * refill_rate)
```

We only need to store two numbers per client: `tokens` and `ts` (last timestamp). This is called *lazy refill*.

### 3. Why Redis?
Later we'll run **3 copies** of the API. If each copy counted in its own memory, a client could get
10 requests from *each* copy = 30. Redis is one shared, very fast in-memory store that all copies talk to,
so there is one bucket per client, globally.

### 4. Why a Lua script? (the race condition)
A naive version does: *read tokens → decide in Python → write tokens back*. With two requests at the same moment:

```
Request A reads tokens = 1          Request B reads tokens = 1
Request A: 1 >= 1, allow, write 0   Request B: 1 >= 1, allow, write 0
```

Two requests passed with only one token. That's a **race condition**.

Redis runs a Lua script **atomically**: while it runs, no other command touches Redis.
So read-decide-write happens as one indivisible step. No race possible.

### 5. Why use Redis's clock (`TIME`) instead of Python's?
Different servers' clocks drift by a few milliseconds or more. If server 1 writes `ts` from its clock and
server 2 reads it with a different clock, the elapsed time is wrong (even negative). Asking Redis for the time
inside the script means everyone uses **one clock**.

### 6. What is middleware?
Code that runs **before every request reaches your route** (and can modify the response after).
Perfect for rate limiting: check the limit first; if blocked, return 429 and the route never runs.

### 7. HTTP 429 and headers
- `429 Too Many Requests` is the standard status code for rate limiting.
- `Retry-After: 3` tells the client "try again in 3 seconds".
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` tell well-behaved clients where they stand,
  so they can slow down *before* being blocked. GitHub's API does exactly this.

### 8. `async` / `await`
FastAPI can handle many requests concurrently on one thread. When code `await`s something slow
(like a network call to Redis), Python serves other requests meanwhile. That's why we use `redis.asyncio`.

---

## Part B — Setup (Phase 0, do this first)

> **Already done for this project (2026-11-02).** Kept here so you understand what was run.
> One difference: the newer `uv` created a `src/` package layout, which was removed in favour of the simpler
> `app/` folder used below, and `pyproject.toml` got `pythonpath = ["."]` so pytest can import `app`.
> Each new session you only need: Docker Desktop open → `docker start redis`.

Run these in the VS Code terminal (PowerShell) from `D:\RateGate`.

**1. Install `uv`** (a fast Python project/package manager):
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
Close and reopen the terminal, then check: `uv --version`.

**2. Create the project:**
```powershell
uv init --app --python 3.12
uv python pin 3.12
```
- `uv init` creates `pyproject.toml` (your project's settings + dependency list), `.python-version`,
  `.gitignore`, `README.md`, a sample `main.py`, and a git repository.
- We pin **3.12** because your system Python (3.15) is very new and some libraries may not support it yet.
  `uv` downloads 3.12 just for this project. Your system Python is untouched.
- **Delete the sample `main.py`** it generated in the root. We'll make our own inside `app/`.

**3. Add dependencies:**
```powershell
uv add fastapi "uvicorn[standard]" redis pydantic-settings
uv add --dev pytest httpx ruff
```
- `fastapi`: the web framework. `uvicorn`: the server that runs it.
- `redis`: Python client for Redis. `pydantic-settings`: reads config from environment variables.
- `--dev` = only needed while developing (tests, linting), not in production.
- This also creates `uv.lock`, which records exact versions so the project installs identically anywhere. Commit it.

**4. Start Redis:**
Open **Docker Desktop** and wait until it says it's running. Then:
```powershell
docker run -d -p 6379:6379 --name redis redis:7
docker exec redis redis-cli ping
```
You should see `PONG`.
- `-d` runs it in the background, `-p 6379:6379` exposes Redis's port to your machine, `--name redis` names it.
- Next time you only need `docker start redis`.

---

## Part C — Build it

Final structure for this phase:
```
RateGate/
  app/
    __init__.py
    config.py
    main.py
    middleware.py
    limiter/
      __init__.py
      token_bucket.py
      scripts/
        token_bucket.lua
  pyproject.toml
```
Create the folders and the two empty `__init__.py` files first. (An `__init__.py` marks a folder as a
Python package so you can write `from app.limiter.token_bucket import ...`.)

### Step 1 — `app/config.py` (settings)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RATEGATE_", env_file=".env")

    redis_url: str = "redis://localhost:6379/0"
    capacity: int = 10  # max tokens in a bucket (burst size)
    refill_rate: float = 1.0  # tokens added per second


settings = Settings()
```

**What's happening:** each field has a default, but can be overridden by an environment variable
with the `RATEGATE_` prefix, e.g. `RATEGATE_CAPACITY=5`. This means you change behaviour without editing code,
which matters a lot once this runs in Docker.

### Step 2 — `app/limiter/scripts/token_bucket.lua` (the heart of the project)

```lua
-- Token bucket, executed atomically inside Redis.
-- KEYS[1] = the bucket key for one client
-- ARGV[1] = capacity, ARGV[2] = refill rate (tokens/sec), ARGV[3] = cost of this request

local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

-- Redis server time: {seconds, microseconds}. Convert to milliseconds.
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- Load the saved state. A brand new client gets a full bucket.
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

-- Lazy refill: add tokens for the time that passed, never above capacity.
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate / 1000)

local allowed = 0
local retry_after_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  -- How long until enough tokens drip in?
  retry_after_ms = math.ceil((cost - tokens) * 1000 / refill_rate)
end

-- Save state. Expire the key once the bucket would be full again:
-- a missing key and a full bucket mean the same thing, so idle clients cost no memory.
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity * 1000 / refill_rate))

local reset_ms = math.ceil((capacity - tokens) * 1000 / refill_rate)
return {allowed, math.floor(tokens), retry_after_ms, reset_ms}
```

**Walk-through:**
- `KEYS` and `ARGV` are how Python passes data into the script. Redis wants key names in `KEYS` specifically.
- The bucket is stored as a Redis **hash** (like a small dictionary): `{tokens: 7.3, ts: 1730530000123}`.
- `tonumber(nil)` is `nil`, which is how we detect "first request ever".
- We return integers because Redis converts Lua decimals to integers anyway. That's why we `floor`/`ceil` explicitly.
- `PEXPIRE` = expire in milliseconds. Without it, every client that ever visited would stay in Redis forever.

### Step 3 — `app/limiter/token_bucket.py` (Python wrapper)

```python
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis

LUA_SCRIPT = (Path(__file__).parent / "scripts" / "token_bucket.lua").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # seconds until the next request would be allowed (0 if allowed)
    reset_after: float  # seconds until the bucket is completely full again


class TokenBucket:
    def __init__(self, redis: Redis, capacity: int, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._script = redis.register_script(LUA_SCRIPT)

    async def check(self, client_id: str, cost: int = 1) -> Decision:
        key = f"rategate:tb:{client_id}"
        allowed, remaining, retry_ms, reset_ms = await self._script(
            keys=[key], args=[self.capacity, self.refill_rate, cost]
        )
        return Decision(
            allowed=bool(allowed),
            limit=self.capacity,
            remaining=int(remaining),
            retry_after=retry_ms / 1000,
            reset_after=reset_ms / 1000,
        )
```

**What's happening:**
- `register_script` sends the script to Redis once. After that, calls send only its short **SHA hash**
  (`EVALSHA`) instead of the full text, which is faster. If Redis restarts and forgets it, the library reloads it automatically.
- `Decision` is a small immutable result object. The middleware doesn't need to know anything about Lua.
  It just reads `decision.allowed`. In Phase 2 every algorithm will return this same `Decision`.
- Key naming `rategate:tb:<client>` is a Redis convention: namespaces separated by colons, so keys are easy to find and never clash.

### Step 4 — `app/middleware.py` (the gatekeeper)

```python
import math

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

EXEMPT_PATHS = {"/health", "/docs", "/openapi.json"}


def identify_client(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        limiter = request.app.state.limiter
        decision = await limiter.check(identify_client(request))

        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(math.ceil(decision.reset_after)),
        }

        if not decision.allowed:
            headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after)))
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Slow down."},
                headers=headers,
            )

        response = await call_next(request)
        response.headers.update(headers)
        return response
```

**What's happening:**
- `dispatch` runs for every request. `call_next(request)` passes it on to the actual route.
- **Who is the client?** An `X-API-Key` header if present (how real APIs identify customers), else the IP address.
  (Phase 5 note: behind nginx every request will look like it comes from nginx's IP. We'll handle that then.)
- `/health` and the docs pages are exempt. You don't want monitoring checks to be rate limited.
- Blocked → we return the 429 **ourselves**, and the route never runs. Allowed → run the route, then attach headers.
- `Retry-After` must be a whole number of seconds, so we round up and never send `0`.

### Step 5 — `app/main.py` (wire it together)

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import settings
from app.limiter.token_bucket import TokenBucket
from app.middleware import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.limiter = TokenBucket(redis, settings.capacity, settings.refill_rate)
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

**What's happening:**
- **`lifespan`**: code before `yield` runs once at startup; code after runs at shutdown.
  We open **one** Redis connection pool for the whole app (not one per request, which would be slow) and close it cleanly.
- `app.state` is a place to store shared objects; the middleware reads `request.app.state.limiter` from it.
- `/api/resource` is a stand-in for "your real API". The rate limiter protects it without the route knowing anything.

---

## Part D — Run and verify

**1. Start the server:**
```powershell
uv run uvicorn app.main:app --reload
```
`app.main:app` means "in file `app/main.py`, the object named `app`". `--reload` restarts the server when you save a file.
Open http://127.0.0.1:8000/docs and you get free interactive API docs from FastAPI.

**2. In a second terminal, fire 15 quick requests:**
```powershell
1..15 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " http://127.0.0.1:8000/api/resource }
```
Expected: about ten `200`s then `429`s. (Use `curl.exe`, not `curl`. In Windows PowerShell, `curl` is a
different command.)

**3. Look at the headers:**
```powershell
curl.exe -i http://127.0.0.1:8000/api/resource
```
Find `X-RateLimit-Remaining`, and on a 429, `Retry-After`.

**4. Watch the refill:** wait 5 seconds, run step 2 again. About 5 succeed, because 5 tokens dripped back.

**5. Look inside Redis** (this is how you debug):
```powershell
docker exec redis redis-cli KEYS "rategate:*"
docker exec redis redis-cli HGETALL "rategate:tb:ip:127.0.0.1"
docker exec redis redis-cli PTTL "rategate:tb:ip:127.0.0.1"
```

**6. Different clients get separate buckets:**
```powershell
1..12 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: alice" http://127.0.0.1:8000/api/resource }
```
Alice gets her own fresh 10, even if your IP is currently blocked.

**7. Change config without changing code:** stop the server (Ctrl+C), then:
```powershell
$env:RATEGATE_CAPACITY = "3"; uv run uvicorn app.main:app --reload
```
Now only 3 requests burst through. (Remove it afterwards with `Remove-Item Env:RATEGATE_CAPACITY`.)

### Bonus: your first automated test — `tests/test_token_bucket.py`
```python
from fastapi.testclient import TestClient
from redis import Redis

from app.config import settings
from app.main import app


def test_blocks_after_capacity():
    Redis.from_url(settings.redis_url).delete("rategate:tb:ip:testclient")

    with TestClient(app) as client:
        codes = [client.get("/api/resource").status_code for _ in range(settings.capacity + 5)]

    assert codes[: settings.capacity] == [200] * settings.capacity
    assert codes[settings.capacity :] == [429] * 5
```
Run with `uv run pytest -v`. (`TestClient` calls itself `testclient` as its IP, and we delete that key first so old state doesn't affect the test.)

---

## Part E — Troubleshooting

| Problem | Likely cause / fix |
|---|---|
| `ConnectionRefusedError` / `Error 10061` | Redis isn't running → start Docker Desktop, `docker start redis` |
| `failed to connect to the docker API` | Docker Desktop isn't open |
| `ModuleNotFoundError: No module named 'app'` | Run commands from `D:\RateGate`, and check `app/__init__.py` exists |
| `FileNotFoundError ... token_bucket.lua` | The `.lua` file is in the wrong folder or misnamed |
| Never get a 429 | Requests are too slow, or capacity is high. Lower `RATEGATE_CAPACITY` |
| Always 429 | Check `HGETALL` for your key; delete it with `docker exec redis redis-cli DEL <key>` |
| `ResponseError: ... script` | Lua syntax error, the message shows the line number |

---

## Part F — Finish the phase

**Done when:** a burst gives 200s then 429s, tokens refill over time, different API keys are independent,
and headers show correct values.

**Commit:**
```powershell
git add .
git commit -m "Phase 1: token bucket rate limiter using atomic Redis Lua script"
```
Then tick the Phase 1 boxes in `ROADMAP.md` and add a line to the progress log.

**Check your understanding** (try to answer in your own words; these are common interview questions):
1. Why can't the token count live in a Python variable once there are 3 servers?
2. Describe the race condition the Lua script prevents.
3. Why use Redis's `TIME` instead of `time.time()` in Python?
4. Why does the key expire, and why is it safe to delete it?
5. If capacity is 10 and refill is 1/sec, how many requests can a client make in the first 60 seconds?
   (Answer: 10 burst + ~60 refilled ≈ 70.)
