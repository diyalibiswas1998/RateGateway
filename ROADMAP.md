# RateGate Roadmap

Each phase has one **intent** (why it exists), the **tasks**, a **done when** check you can verify yourself,
and **what you learn**. Finish a phase, verify it, commit, then move on. Never start a phase with the previous one broken.

**Working loop for every phase:** understand → build the smallest thing → test it → run it → commit → note what you learned.

---

## Phase 0 — Setup
**Intent:** a reproducible environment, so "works on my machine" never happens.

- [x] Install `uv` (`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)
- [x] `git init` + `.gitignore` (done by `uv init`)
- [x] Create a public GitHub repo and push
- [x] `uv init`, pin Python 3.12 (`uv python pin 3.12`)
- [x] Add deps: `fastapi uvicorn[standard] redis pydantic-settings`; dev: `pytest httpx ruff`
- [x] Run Redis in Docker: `docker run -d -p 6379:6379 --name redis redis:7`

**Done when:** `docker exec redis redis-cli ping` returns `PONG` and `uv run python -c "import fastapi, redis"` works.
**You learn:** dependency management, lockfiles, containers.

---

## Phase 1 — One API, one algorithm (token bucket)
**Intent:** prove the core idea end to end before adding abstractions.

- [x] FastAPI app with a `/api/resource` demo route and `/health`
- [x] Token bucket as a **Lua script** (read tokens, refill by elapsed time, take one, write back — all atomic)
- [x] Use `redis.call('TIME')` inside Lua, never the app server's clock
- [x] Middleware: identify the client (API key header, fallback to IP), call the limiter
- [x] Blocked → `429` + `Retry-After`; always send `X-RateLimit-Limit / Remaining / Reset`

**Done when:** hitting the endpoint in a loop gives 200s then 429s, and tokens refill after waiting.
**You learn:** token bucket math, Lua atomicity, FastAPI middleware, HTTP semantics.

---

## Phase 2 — Strategy interface + two more algorithms
**Intent:** make algorithms swappable so you can compare their trade-offs.

- [x] `RateLimitStrategy` abstract class returning a `Decision(allowed, remaining, reset_after, retry_after)`
- [x] Refactor token bucket into it
- [x] Sliding window log (Redis sorted set: `ZREMRANGEBYSCORE` + `ZCARD` + `ZADD`)
- [x] Sliding window counter (current + previous window, weighted)
- [x] Choose algorithm + limits via config / env vars
- [x] Write in the README: memory cost vs accuracy vs burst behaviour of each

**Done when:** switching `RATEGATE_ALGORITHM` changes behaviour with no code edits; each algorithm has unit tests.
**You learn:** strategy pattern, Redis data structures, algorithm trade-offs.

---

## Phase 3 — Correctness under concurrency
**Intent:** prove the "no race conditions" claim with evidence, not words.

- [x] Tests run against **real Redis** (not fakeredis — its Lua support is partial)
- [x] Concurrency test: limit 100, fire 200 simultaneous requests → exactly 100 allowed
- [x] Boundary tests: refill timing, window edges, key expiry (`PEXPIRE` so idle clients don't leak memory)

**Done when:** `uv run pytest` is green, and the concurrency test passes repeatedly.
**You learn:** race conditions, why atomic scripts matter, writing meaningful tests.

---

## Phase 4 — Failure handling (fail-open / fail-closed)
**Intent:** decide what happens when Redis is down, instead of letting the app crash.

- [x] Short Redis socket/connect timeouts (e.g. 50–100 ms) — measured instead: 0.25 s per command, 1 s to connect (connects take up to ~600 ms under a burst; 100 ms broke the concurrency tests)
- [x] `RATEGATE_FAIL_MODE=open` → allow + log warning; `closed` → `503`
- [x] Test: stop Redis mid-test and assert each mode's behaviour — automated tests simulate the outage with a closed port (including mid-sequence and recovery); real `docker stop` / `docker pause` checked by hand

**Done when:** `docker stop redis` → API still responds according to the configured mode.
**You learn:** resilience design, availability vs protection trade-off.

---

## Phase 5 — Horizontal scaling
**Intent:** demonstrate the headline claim — limits hold across multiple instances.

- [x] `Dockerfile` for the API
- [x] `docker-compose.yml`: Redis + 3 API replicas + nginx (round-robin) — needed `zone` so nginx's 16 workers share one round-robin count, and `proxy_connect_timeout 1s` (a stopped copy cost 38.8 s)
- [x] Each response includes an `X-Instance-Id` header
- [x] Test/script: requests spread across all 3 instances still stop at exactly the limit — `scripts/sequential_requests.py`, `scripts/concurrent_burst.py`; client IP via `X-Forwarded-For` (nginx overwrites it, uvicorn `--proxy-headers`)

**Done when:** `docker compose up` → responses come from different instances, but the client is limited once, globally.
**You learn:** shared state, load balancing, container orchestration basics.

---

## Phase 6 — CI with GitHub Actions
**Intent:** every push is automatically checked.

- [x] Workflow: install `uv` → `ruff check` → `ruff format --check` → `pytest` — `.github/workflows/ci.yml`; `astral-sh/setup-uv` has no `v10` tag, so it's pinned to `v10.3.3`
- [x] Redis as a **service container** in the workflow — plus a second job that starts the full Docker stack and checks a burst through nginx allows exactly 100 across 3 copies
- [x] CI badge in README

**Done when:** a pushed commit shows a green check on GitHub; a deliberately broken test turns it red.
**You learn:** CI pipelines, service containers.

---

## Phase 7 — Load testing with Locust
**Intent:** measure, don't guess.

- [x] `loadtest/locustfile.py` with several simulated clients — 40 polite, 10 greedy, 5 health checks; plus `capacity.py` (clients that never pause)
- [x] Run against the 3-replica compose setup — Locust runs as a Compose service on the stack's network (`docker compose run --no-deps`); capacity also with 1 and 6 copies
- [x] Record RPS, p50/p95 latency, % 429s per algorithm → table in README

**Done when:** README contains real numbers from your machine.
**You learn:** load testing, latency percentiles, reading results honestly.

---

## Phase 8 — Polish (optional)
- [x] `/metrics` with `prometheus-client` (allowed / blocked / fail-open counters) — `rategate_decisions_total{outcome, tier}` (also fail-closed) and a Redis-check timer; per copy, hidden behind nginx; `scripts/collect_metrics.py` adds the copies up
- [x] Architecture diagram + "design decisions" section in README — a Mermaid flowchart and a decisions table gathered from every phase
- [x] Per-route or per-tier limits (e.g. free vs paid API keys) — per tier: keys in `RATEGATE_PAID_API_KEYS` get 10 times the free limits; `X-RateLimit-Tier` header

---

## Progress log
| Date | Phase | What I learned / problems hit |
|------|-------|-------------------------------|
| 2026-11-03 | 8 | Tiers (free / paid by API key, 10x limits), Prometheus metrics (decisions by outcome and tier, Redis-check timer), architecture diagram and design decisions in the README. 47 tests. Summed per-copy counters matched client results exactly; load-test latency unchanged with metrics on. Learned: metrics are per copy (monitoring adds them up), never label by API key, and build tiers before metrics so the metrics tests fail for the right reason |
| 2026-11-03 | 7 | Locust: 230 req/s mixed traffic, p50 4 ms / p95 6–7 ms; polite clients never limited; greedy clients get 0.88–1.02/s of the configured 1/s. Capacity: 1 copy ~740 served/s (with 502s), 3 copies 1,920/s, 6 copies 2,730/s; the Python copies are the bottleneck, Redis under 30%. Traps: users started in lockstep looked 8× slower; `docker compose run` without `--no-deps` silently recreated the copies with default settings; one Locust process maxed out first |
| 2026-11-03 | 6 | GitHub Actions: lint, format, 40 tests against a Redis service container (production timeouts, no `.env`), and the Phase 5 stack checked on every push (burst → exactly 100 across 3 copies). 4 of 4 trial runs green in about 30–90 s; a deliberately broken test turned it red on a temporary branch. Learned: not every action has a short major tag (`setup-uv@v10` doesn't exist); a check script must exit non-zero to fail CI |
| 2026-11-02 | 5 | 3 API copies behind nginx share one Redis: 200 requests at once, limit 100 → exactly 100 for every algorithm (~33 per copy); with separate state per copy, 300 of 400. A stopped copy: 300/300 still answered, ≤1.01 s. 40 tests. Found and fixed a Phase 4 bug: retrying after a timeout counted a request twice (20 late replies → 40 counts); now only connection errors are retried, which also halves outage detection (hung 0.25 s, stopped 1 s). Learned: nginx workers need `zone` to share round-robin; never trust a client's `X-Forwarded-For`; Windows → Docker Desktop is slower than the real network, so this machine gets its own timeouts in `.env` |
| 2026-11-02 | 4 | Fail-open (200, warning) / fail-closed (503 + Retry-After: 5); hung Redis detected in ~0.5 s instead of a 5 s freeze; recovers with no restart; 36 tests. Learned: measure before choosing timeouts (connect ≠ command), one immediate retry handles stale connections, and check new tests fail on old code (one passed for the wrong reason) |
| 2026-11-02 | 3 | 31 tests: 200 concurrent requests → exactly 100 for every algorithm; a non-atomic control lets 200/200 through. Tests found 2 bugs: the default Redis pool raised `MaxConnectionsError` under a burst (HTTP 500s), fixed with `BlockingConnectionPool`; the sliding counter's `Retry-After` was too short, fixed in Lua. Learned: write the failing test first, and repeat concurrency tests |
| 2026-11-02 | 2 | Three algorithms behind `RateLimitStrategy`, chosen by `RATEGATE_ALGORITHM`; middleware untouched; 16 tests pass. Learned: the sliding log returns a burst's allowance all at once a window later, and the sliding counter's results depend on where the window boundary falls. Also: ruff formats code inside Markdown, so `docs/` is excluded |
| 2026-11-02 | 1 | Token bucket works: burst of 10, refill 1/s, separate buckets per client. Learned: Redis keys expire 10 s after the last request, so inspect Redis on the same line as the requests. Committed `d7687fa` |
| 2026-11-02 | 0 | Setup with uv, Python 3.12 (system 3.15 too new), Redis in Docker, pushed to GitHub. Learned: Redis port 6379 isn't a website, so the browser shows nothing |
