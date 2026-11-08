# Phase 7 Guide — Load Testing with Locust

**Goal of this phase:** measure how RateGate behaves under real load — how many requests per second it serves,
how fast it answers, and whether it still limits *exactly* right when hundreds of requests a second arrive —
and put the real numbers in the README.

This proves the resume line *"load-tested with Locust"* and gives you numbers to quote instead of adjectives.

**What we measured** (2026-11-03, this laptop: Intel Core i5-14450HX, 16 threads, 16 GB RAM):

| Result | Evidence |
|---|---|
| Correct under load | 40 clients under their limit: **0** of 1,180 requests refused. 10 clients asking for 20×: allowed **1.00–1.02 per second** each (configured: 1) |
| Fast | 230 requests/s: p50 4 ms, p95 7 ms, for all three algorithms |
| The limiter costs nothing measurable here | `/health`, which skips Redis, also answers in 4 ms (p50) |
| Algorithms differ where theory says | Sliding log exactly 1.00/s; counter 0.89/s (its estimate is stricter); bucket 1.02/s |
| Scaling out adds capacity | 1 copy ≈ 740 served/s (with `502` errors); 3 copies 1,940/s; 6 copies 2,910/s |
| The bottleneck is the Python copies | Each copy at ~1 CPU core while Redis stayed under 30% |

Three traps distorted the first measurements, and each would have made the README wrong (Concept 6). They're
the most useful part of this phase.

---

## Part A — Concepts (read before coding)

### 1. What load testing is, and what it measures

A **load test** sends many requests, from many simulated users, for a while, and records what happens. Unit
tests answer "is it correct?"; a load test answers "is it still correct, and how fast, **under pressure**?".

Three numbers matter:

| Number | Meaning | RateGate's question |
|---|---|---|
| **Throughput** — requests per second (RPS) | How much work gets done | How much traffic can 3 copies serve? |
| **Latency** — response time | How long one request takes, from sending to the full answer | Does checking Redis slow requests down? |
| **Error rate / outcome mix** | How many requests failed, and how | Do polite clients ever get refused? Do greedy ones get exactly their limit? |

### 2. Percentiles: why averages lie

Response times are never all the same. Sort 100 of them from fastest to slowest:

- **p50** (the median) — the 50th: half of the requests were faster. "The typical request."
- **p95** — the 95th: 95% were faster, 5% slower. "The slow ones a user notices now and then."
- **p99** — 99% were faster. "The worst 1 in 100."

Why not the average? One very slow request drags the average up while hiding *how many* requests were slow;
and a few very fast ones can hide a slow tail. Example: 95 requests take 5 ms and 5 take 500 ms. The average is
about 30 ms — which matches **no** request anyone saw. p50 = 5 ms and p95/p99 ≈ 500 ms tell the real story:
most are fast, and 1 in 20 is very slow. Real services are judged by percentiles for this reason.

### 3. Locust: the vocabulary

**Locust** is a free load-testing tool where users are written as Python classes.

| Word | Meaning | In RateGate |
|---|---|---|
| **User** (class) | One kind of simulated client; Locust runs many instances | `PoliteClient`, `GreedyClient`, `HealthCheck`, `FloodClient` |
| **Task** | What a user does, again and again (`@task`) | `GET /api/resource` with its own API key |
| **`wait_time`** | The pause between a user's tasks | `constant_throughput(0.5)`: one task every 2 s |
| **`fixed_count`** | Exactly this many users of a class | 40 polite, 10 greedy, 5 health |
| `-u` (users) / `-r` (spawn rate) | How many users in total / how many to start per second | `-u 55 -r 5` |
| `-t` | How long to run | `-t 70s` |
| `--headless` | No web page; print results and stop at the end | Repeatable runs |
| `--reset-stats` | Throw away the numbers collected while users were still starting | Measure full load only |
| `--csv PREFIX` | Write results to `PREFIX_stats.csv` and friends | Turned into the README table |
| `--processes N` | Run N Locust processes, for more load than one CPU core can generate | Capacity test |

Locust's statistics are grouped by request **name**. We give each outcome its own name — `polite allowed`,
`polite limited`, `greedy allowed`, `greedy limited` — so one run tells apart what a table of totals would mix.

**A 429 is not a failure here.** Locust treats anything but 2xx as a failure by default. For a rate limiter, a
429 is the feature *working*, so the locustfile marks 429s as successes (in their own row) and only counts truly
unexpected answers (500, 502, …) as failures.

### 4. The scenario, and what we expect *before* running it

Decide the expected numbers first; otherwise any output looks plausible. Default limits: a 10-token burst and
1 token per second (sliding windows: 10 per 10 s — the same 1 per second).

| Client | How many | Pace | Expected |
|---|---|---|---|
| **Polite** | 40 | 1 request every 2 s (half its limit) | Never refused: **0 limited** |
| **Greedy** | 10 | 20 requests/s (20 × its limit) | Allowed ≈ **1 per second each**; ~95% refused |
| **Health** | 5 | 2 requests/s, `/health` (no limiter) | A baseline: same path, without the Redis check |

Expected total: 40 × 0.5 + 10 × 20 + 5 × 2 = **230 requests per second**. If the measured rate is lower, the
system (or the load generator) can't keep up.

**Capacity test** (a separate file): clients that never pause, sending the next request the moment the last
answer arrives. That finds the ceiling — the most requests per second the stack can serve.

### 5. Where the load generator runs matters

Locust could run on Windows and hit `localhost:8080`. But in Phase 5 we saw that Docker Desktop's port
forwarding adds delay. So Locust runs **as a container on the stack's network**, talking to `nginx:80` like any
other service, and its numbers describe RateGate, not Windows. Measured, same test both ways:

| Locust runs… | p50 | p95 | p99 (polite) | Polite limited | Per greedy client |
|---|---|---|---|---|---|
| On Windows (through port forwarding) | 7 ms | 20 ms | 46 ms | 0 | 1.02/s |
| Inside Docker (RateGate's numbers) | 4 ms | 7 ms | 13 ms | 0 | 1.02/s |

The behaviour is identical; only the timing changes. Running Locust as a container also means nothing to
install: the official image `locustio/locust:2.47.0` has it.

### 6. Reading results honestly: the three traps we hit

**Trap 1 — the test itself created a traffic jam ("thundering herd").** The first run started all 55 users
within one second. Polite clients then took a typical **32 ms** — eight times the greedy clients' 4 ms, though
they send 40 times less. A single client measured alone saw 2–5 ms, so the server wasn't slow. The cause: all
40 polite clients started together and each fired exactly every 2 s, so their requests kept arriving **at the
same instant** and queued behind each other. Starting users gradually (`-r 5`, 5 per second) spreads them out:
polite p50 fell to **6 ms**. Real users don't arrive in lockstep; a test that makes them do so measures itself.

**Trap 2 — the tool quietly changed what was being tested.** `docker compose run locust` also starts the
service's dependencies — and **recreated the API copies from the Compose file**, dropping the
`RATEGATE_ALGORITHM` they had been started with and undoing `--scale`. So the first "sliding log" and "sliding
counter" runs actually tested the token bucket again (all three rows came out identical — suspicious in
itself), and the scaling runs had the wrong number of copies. It was caught by checking: container IDs changed
after the run, and `RATEGATE_ALGORITHM` was gone. The fix is `--no-deps`: start Locust only, touch nothing
else. Each run now also checks the **Redis keys** the algorithm leaves behind (`tb:`, `swl:`, `swc:`) — proof
of which algorithm really ran.

**Trap 3 — the load generator ran out of power first.** The first capacity runs gave ~1,050 requests/s with
both 20 and 100 users, and Locust warned `CPU usage above 90%`. When doubling the users doesn't raise the
throughput, either the system or the **tool** is saturated — here the tool: one Locust process uses one CPU
core. With `--processes 4` the same test reached ~2,050/s. Always check the generator isn't the bottleneck before
blaming the system.

### 7. Finding the bottleneck

During a capacity run, `docker stats` shows each container's CPU use (100% = one full core):

| 3 copies, full load | CPU |
|---|---|
| Each API copy | 97–116% |
| nginx | 82% |
| Redis | 23% |
| Locust (6 processes) | ~310% |

Each copy is **one Python process**, which can use about one core — and all three were at that limit, while
Redis was mostly idle. So the API copies are the bottleneck, and adding copies should add capacity. It did:

| API copies | Served per second | Errors | p95 |
|---|---|---|---|
| 1 | about 740 | 5,400–7,700 `502`s | 220–230 ms |
| 3 | 1,940 | 0 | 230 ms |
| 6 | 2,910 | 0 | 120 ms |

- **1 copy:** overloaded, it couldn't accept new connections within nginx's 1 s connect timeout; nginx's log
  shows `upstream timed out` and `upstream server temporarily disabled`, and with no other copy to try it
  answered `502` instantly for a while. Served requests only: about 740/s. A single copy is also a single
  point of failure.
- **3 copies:** 2.6 times the single copy, and no errors.
- **6 copies:** more again, but Locust's 6 processes were then near their limit (~5 cores busy), so 2,910 is a
  lower bound, not the stack's ceiling.

Everything — Locust, nginx, the copies and Redis — shares one laptop's 16 threads, so these are **this
machine's** numbers. On separate servers, each part would have its own CPUs.

**Latency at saturation measures queueing.** When clients never pause, every request waits behind others, so
p50/p95 in the capacity runs (16–230 ms) say "the system is full", not "the system is slow". The 4 ms / 7 ms from
the mixed-traffic run are RateGate's response times under normal load.

---

## Part B — Setup

1. Docker Desktop running; your Phase 0 Redis running (for `uv run pytest`).
2. The stack built and up: `docker compose up --build -d --wait`.
3. Nothing to install: the first `docker compose run ... locust` downloads `locustio/locust:2.47.0` (about 100 MB).

---

## Part C — Build it (in this order)

Files touched (★ new, ✎ changed):

```
loadtest/
  locustfile.py        ★ Step 1 (polite, greedy, health)
  capacity.py          ★ Step 1 (flood clients, for the capacity test)
  summarize.py         ★ Step 3 (CSV → Markdown table)
docker-compose.yml     ✎ Step 2 (a locust service, only started on request)
.gitignore             ✎ Step 2 (loadtest/results/)
README.md              ✎ Step 7 (Load testing section)
ROADMAP.md             ✎ Step 8
```

| Step | What | Why here |
|---|---|---|
| 1 | Write the users: `locustfile.py`, `capacity.py` | The scenario, decided before measuring |
| 2 | A `locust` service in Compose; ignore results in git | Run Locust inside the stack's network |
| 3 | `summarize.py` | Turn CSVs into the README table the same way every time |
| 4 | A short smoke run | Check everything runs before spending minutes on real runs |
| 5 | Mixed traffic, per algorithm | Correctness and latency under load |
| 6 | Capacity, with 1, 3 and 6 copies | Throughput and the bottleneck |
| 7 | README | Real numbers, with the conditions they were measured under |
| 8 | ROADMAP | Record it |

### Step 1 — The simulated users

**`loadtest/locustfile.py`:**

```python
"""Load test: polite clients stay under their limit, greedy ones ask for 20 times it.

Run it inside the stack's network (see docker-compose.yml), for example:
    docker compose run --no-deps --rm locust -f /mnt/loadtest/locustfile.py --headless \
        -u 55 -r 5 -t 70s --reset-stats --host http://nginx:80 \
        --csv /mnt/loadtest/results/token_bucket
"""

from uuid import uuid4

from locust import HttpUser, constant_throughput, task


class RateGateClient(HttpUser):
    abstract = True  # a template for the client types below, never run itself
    kind = ""

    def on_start(self):
        # Each simulated client has its own API key, so its own limit.
        self.api_key = f"{self.kind}-{uuid4().hex[:8]}"

    @task
    def call_api(self):
        with self.client.get(
            "/api/resource", headers={"X-API-Key": self.api_key}, catch_response=True
        ) as response:
            if response.status_code == 200:
                outcome = "allowed"
            elif response.status_code == 429:
                outcome = "limited"
            else:
                response.failure(f"unexpected status {response.status_code}")
                return
            # A 429 is the limiter working, not an error: count it as a success,
            # but in its own row, so the statistics show allowed and limited apart.
            response.request_meta["name"] = f"{self.kind} {outcome}"
            response.success()


class PoliteClient(RateGateClient):
    """1 request every 2 seconds: half of the 1-per-second refill rate."""

    kind = "polite"
    fixed_count = 40
    wait_time = constant_throughput(0.5)


class GreedyClient(RateGateClient):
    """20 requests a second: 20 times what its limit allows."""

    kind = "greedy"
    fixed_count = 10
    wait_time = constant_throughput(20)


class HealthCheck(HttpUser):
    """/health skips the limiter: the same path without the Redis check, for comparison."""

    fixed_count = 5
    wait_time = constant_throughput(2)

    @task
    def health(self):
        self.client.get("/health", name="health (no limiter)")
```

**Piece by piece:**

- **`HttpUser`** — Locust's base class for a user that sends HTTP requests. `self.client` is its HTTP client;
  it keeps its connection open between requests, like a browser.
- **`abstract = True`** — `RateGateClient` is a template: it holds what polite and greedy clients share, and
  Locust never runs it directly.
- **`kind`** — a class attribute each subclass sets (`"polite"`, `"greedy"`), used in the API key and the row names.
- **`on_start`** — runs once when a user starts. Each gets a random API key (`uuid4`), so **each simulated client
  has its own bucket**, like separate real customers.
- **`@task`** — marks the method Locust calls repeatedly.
- **`catch_response=True` + `with ... as response:`** — lets *us* decide whether the request succeeded, instead of
  Locust's default (anything but 2xx = failure).
- **`response.request_meta["name"] = ...`** — the name the request is recorded under. Locust records the request
  when the `with` block ends, using this dictionary, so changing the name inside the block files it under
  `greedy limited` etc. (`request_meta` is a public attribute in Locust 2.47's source, but this use isn't in
  Locust's documentation — we checked the source and confirmed it with a real run; recheck if you upgrade Locust.)
- **`response.success()` / `response.failure(...)`** — record the outcome. Only unexpected statuses are failures.
- **`fixed_count`** — exactly this many users of this class. 40 + 10 + 5 = 55, so run with `-u 55`.
- **`constant_throughput(0.5)`** — Locust picks each pause so the task runs 0.5 times per second, whatever the
  response time. Unlike a fixed pause, the rate stays exact even when responses slow down.
- **`name="health (no limiter)"`** — a plain request with its own row name.

**`loadtest/capacity.py`** — a separate file, so the normal test never includes flood clients:

```python
"""Capacity test: clients that never pause, to find how many requests a second the stack handles.

Kept apart from locustfile.py so the normal test never includes it by accident. Run it
with several Locust processes, so the load generator isn't the bottleneck:
    docker compose run --no-deps --rm locust -f /mnt/loadtest/capacity.py --headless \
        -u 150 -r 150 -t 40s --processes 6 --reset-stats --host http://nginx:80
"""

from locust import constant
from locustfile import RateGateClient


class FloodClient(RateGateClient):
    """Sends the next request as soon as the last answer arrives."""

    kind = "flood"
    wait_time = constant(0)
```

- **`from locustfile import RateGateClient`** — reuses the template. Locust runs every non-abstract user class
  it finds in the file it's given; `PoliteClient` etc. aren't imported here, so only `FloodClient` runs.
- **`constant(0)`** — no pause at all.

### Step 2 — A `locust` service in Compose, and results out of git

At the end of `docker-compose.yml`:

```yaml
  # Load generator (Phase 7), only started on request:
  #   docker compose run --no-deps --rm locust -f /mnt/loadtest/locustfile.py ...
  # --no-deps matters: without it, "run" recreates the API copies from this file,
  # undoing --scale and any RATEGATE_* settings they were started with.
  # It runs on the stack's network, so its numbers don't include Windows' port forwarding.
  locust:
    image: locustio/locust:2.47.0
    profiles: ["loadtest"]
    volumes:
      - ./loadtest:/mnt/loadtest
    depends_on:
      - nginx
```

- **`image: locustio/locust:2.47.0`** — Locust's official image, pinned. Its program is `locust`, so whatever you
  put after `docker compose run ... locust` is passed to Locust as options.
- **`profiles: ["loadtest"]`** — a service with a profile is **not** started by a plain `docker compose up`. So the
  normal stack, and the CI job from Phase 6, are unchanged. `docker compose run locust` starts it on request.
- **`volumes: - ./loadtest:/mnt/loadtest`** — your `loadtest` folder appears inside the container at
  `/mnt/loadtest`: Locust reads the locustfiles from there, and the CSV results it writes there appear in your
  `loadtest/results/` folder.
- **`depends_on: - nginx`** — documents what it talks to. We always run it with `--no-deps` (Trap 2).

In `.gitignore`:

```
# Load-test output (CSV files written by Locust); the summary goes in the README
loadtest/results/
```

Raw CSVs differ every run and would clutter git; the README holds the summary.

### Step 3 — `loadtest/summarize.py`

```python
"""Turn Locust's CSV results into a Markdown table, one row per run.

Usage: uv run python loadtest/summarize.py "Token bucket=loadtest/results/token_bucket" ...
Each argument is a label and the --csv prefix Locust was given.
"""

import argparse
import csv


def load(prefix: str) -> dict[str, dict[str, str]]:
    with open(f"{prefix}_stats.csv", newline="") as f:
        return {row["Name"]: row for row in csv.DictReader(f)}


def count(rows: dict, name: str) -> int:
    return int(rows[name]["Request Count"]) if name in rows else 0


def rate(rows: dict, name: str) -> float:
    return float(rows[name]["Requests/s"]) if name in rows else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", help="LABEL=CSV_PREFIX")
    parser.add_argument("--greedy-clients", type=int, default=10)
    args = parser.parse_args()

    print(
        "| Algorithm | Requests/s | p50 | p95 | Limited (429) "
        "| Polite clients limited | Allowed per greedy client |"
    )
    print("|---|---|---|---|---|---|---|")
    for run in args.runs:
        label, prefix = run.split("=", 1)
        rows = load(prefix)
        total = rows["Aggregated"]
        limited = count(rows, "polite limited") + count(rows, "greedy limited")
        answered = (
            limited + count(rows, "polite allowed") + count(rows, "greedy allowed")
        )
        polite = count(rows, "polite allowed") + count(rows, "polite limited")
        greedy_rate = rate(rows, "greedy allowed") / args.greedy_clients
        print(
            f"| {label} | {float(total['Requests/s']):.0f} "
            f"| {total['50%']} ms | {total['95%']} ms "
            f"| {100 * limited / answered:.1f}% "
            f"| {count(rows, 'polite limited')} of {polite} "
            f"| {greedy_rate:.2f}/s |"
        )


if __name__ == "__main__":
    main()
```

- **`csv.DictReader`** — reads the CSV's header row and turns each following row into a dictionary keyed by
  column name (`"Request Count"`, `"Requests/s"`, `"50%"`, `"95%"`…). Keying rows by `"Name"` lets us look up
  `rows["greedy allowed"]`.
- **`count` / `rate` return 0 for a missing row** — a perfect run has no `polite limited` row at all.
- **`"Aggregated"`** — Locust's total row, across all request names (including the health checks).
- **Allowed per greedy client** = the `greedy allowed` rate ÷ 10 clients: directly comparable to the configured
  1 per second.
- **`nargs="+"`** — one or more `LABEL=PREFIX` arguments, so one command prints all three algorithms' rows.

It only uses Python's standard library, so it runs with `uv run python` and needs nothing installed.

### Step 4 — Smoke run

```powershell
docker compose up --build -d --wait
docker compose run --no-deps --rm locust -f /mnt/loadtest/locustfile.py --headless -u 55 -r 5 -t 40s --reset-stats --host http://nginx:80 --csv /mnt/loadtest/results/check --only-summary
uv run python loadtest/summarize.py "Token bucket=loadtest/results/check"
```

- **`--host http://nginx:80`** — the address *inside* the network: the service name and nginx's own port.
- **`-u 55 -r 5`** — 55 users, started 5 per second (11 s ramp-up; Trap 1).
- **`--reset-stats`** — the ramp-up isn't measured.
- **`--only-summary`** — print only the final table, not a line every few seconds.

Ours (30 s measured): `230` requests/s, p50 4 ms, p95 6 ms, `0 of 580` polite limited, `1.04/s` per greedy client.

### Step 5 — Mixed traffic, per algorithm

```powershell
foreach ($alg in "token_bucket", "sliding_log", "sliding_counter") {
    $env:RATEGATE_ALGORITHM = $alg
    docker compose up -d --wait
    docker compose restart nginx
    docker compose exec redis redis-cli flushall
    docker compose run --no-deps --rm locust -f /mnt/loadtest/locustfile.py --headless -u 55 -r 5 -t 70s --reset-stats --host http://nginx:80 --csv /mnt/loadtest/results/$alg --only-summary
    docker exec rategate-api-1 printenv RATEGATE_ALGORITHM
    docker compose exec redis redis-cli --scan --pattern "rategate:*" | Select-Object -First 3
}
Remove-Item Env:RATEGATE_ALGORITHM
docker compose up -d --wait
docker compose restart nginx
uv run python loadtest/summarize.py "Token bucket=loadtest/results/token_bucket" "Sliding window log=loadtest/results/sliding_log" "Sliding window counter=loadtest/results/sliding_counter"
```

- **`foreach ($alg in ...) { ... }`** — runs the block once per algorithm, with `$alg` set to each name.
- **`$env:RATEGATE_ALGORITHM = $alg` + `docker compose up -d --wait`** — recreates the copies with that algorithm
  (Phase 5's pass-through).
- **`restart nginx`** — recreated copies may have new addresses (Phase 5).
- **`flushall`** on the stack's Redis — each run starts clean.
- **`printenv` and the key scan** — the proof of what actually ran (Trap 2): `tb:`, `swl:` or `swc:` keys.
- About 4 minutes in total.

Ours — each algorithm verified by its keys (50 `tb`, 96 `swl`, 50 `swc`):

| Algorithm | Requests/s | p50 | p95 | Limited (429) | Polite clients limited | Allowed per greedy client |
|---|---|---|---|---|---|---|
| Token bucket | 230 | 4 ms | 7 ms | 86.3% | 0 of 1180 | 1.02/s |
| Sliding window log | 230 | 4 ms | 7 ms | 86.4% | 0 of 1180 | 1.00/s |
| Sliding window counter | 230 | 4 ms | 7 ms | 86.9% | 0 of 1180 | 0.89/s |

**Reading it:**

- **230/s** = the planned 230: the system kept up.
- **0 polite clients limited** — never a false refusal, for any algorithm.
- **Per greedy client:** the configured rate is 1/s. The **log** is exact (1.00) — it remembers every request.
  The **bucket** is a little above (1.02) because each client also used its initial burst of 10 during the 60 s.
  The **counter** is below (0.89): its weighted estimate of the previous window over-counts when traffic is
  constant and heavy — it errs on the strict side, as Phase 2 described.
- **86–87% limited** is just the traffic mix: the greedy clients send 200 of the 230 requests per second.
- **Same latency for all three** — at this load, Redis answers any of the scripts in well under a millisecond;
  differences between algorithms are in memory use and accuracy, not speed.

### Step 6 — Capacity, with 1, 3 and 6 copies

```powershell
foreach ($n in 1, 3, 6) {
    docker compose up -d --scale api=$n --wait
    docker compose restart nginx
    docker compose run --no-deps --rm locust -f /mnt/loadtest/capacity.py --headless -u 150 -r 150 -t 40s --processes 6 --reset-stats --host http://nginx:80 --csv /mnt/loadtest/results/scale_api$n --only-summary
}
docker compose up -d --scale api=3 --wait
docker compose restart nginx
```

While a run is going, in a **second terminal**: `docker stats --no-stream` — each container's CPU at that moment.

- **`--processes 6`** — six Locust processes share the 150 users, so the generator can produce enough load (Trap 3).
- **`--scale api=$n`** then **`restart nginx`** — Phase 5's scaling, with nginx looking up the copies again.
- **`--no-deps`** — essential here: without it, `run` would reset the copies to 3 (Trap 2).

The results are the table in Concept 7. To read a capacity run's CSV directly (failures included):

```powershell
Import-Csv loadtest\results\scale_api3_stats.csv | Select-Object Name, "Request Count", "Failure Count", "Requests/s", "50%", "95%" | Format-Table
```

`Import-Csv` is PowerShell's CSV reader; `Select-Object` picks columns; `Format-Table` lines them up.

### Step 7 — README

A **Load testing** section with the two tables, the machine, the date, the limits, and how to rerun — numbers
without their conditions mean nothing. (Ours is in the README; copy its shape, use your numbers.)

### Step 8 — ROADMAP

Tick the three Phase 7 items and add a progress-log row.

**Optional — watch it live.** Locust has a web page with live charts:

```powershell
docker compose run --no-deps --rm -p 8089:8089 locust -f /mnt/loadtest/locustfile.py --host http://nginx:80
```

Open `http://localhost:8089`, enter 55 users and a spawn rate of 5, and press Start. `-p 8089:8089` publishes the
web page's port; Ctrl+C stops it.

---

## Part D — Verify

| Check | Expected |
|---|---|
| 1. Smoke run | Ends with `exit code 0`; `summarize.py` prints a row |
| 2. Mixed traffic, each algorithm | 230 req/s; 0 polite limited; about 1/s per greedy client; `0` failures |
| 3. The right algorithm ran | `printenv` shows it; keys start with `rategate:tb:` / `swl:` / `swc:` |
| 4. Capacity | More copies → more served requests/s; 3 copies with no errors |
| 5. Bottleneck | `docker stats`: copies near 100%, Redis far below |
| 6. Nothing else broke | `uv run pytest` → `40 passed`; `docker compose up` (no profile) starts 5 containers, no Locust |
| 7. Results not in git | `git status` doesn't list `loadtest/results/` |

---

## Part E — Common pitfalls

| Pitfall | Symptom | Why | Fix |
|---|---|---|---|
| Starting all users at once | Some clients look 5–8 times slower | They fire in lockstep and queue (thundering herd) | `-r 5`; `--reset-stats` |
| `docker compose run` without `--no-deps` | All algorithms give identical results; scaling seems not to work | `run` recreates dependencies from the Compose file | `--no-deps`; check `printenv` and Redis keys |
| One Locust process | Throughput stops growing; `CPU usage above 90%` warning | The generator is the bottleneck | `--processes N` |
| Counting 429 as a failure | "86% errors" | Locust's default: non-2xx = failure | `catch_response`, `response.success()`, own row names |
| Measuring through Windows' port forwarding | Higher, noisier latency | Docker Desktop's forwarding (Phase 5) | Run Locust on the stack's network |
| Trusting the average | A misleading "typical" time | A few slow requests distort it | Look at p50, p95, p99 |
| Reading latency at saturation as "slow" | 100+ ms p95 in capacity runs | Requests queue when the system is full | Use mixed-traffic numbers for response time |
| `-u` not matching the `fixed_count`s | Fewer users of some kinds than planned | Locust fills fixed counts with the users it has | `-u 55` (40 + 10 + 5) |
| Forgetting `restart nginx` after scaling | New copies idle, or errors | nginx looked up the copies at startup (Phase 5) | `docker compose restart nginx` |
| Git Bash path conversion | `Could not find 'C:/Program Files/Git/mnt/...'` | Git Bash rewrites `/mnt/...` | Use PowerShell, or `MSYS_NO_PATHCONV=1` in Git Bash |
| Numbers without conditions | Nobody can compare or reproduce them | Load results depend on the machine and setup | State machine, date, limits, scenario |

---

## Part F — Finish the phase

**Done when:** the README contains real numbers from your machine (the roadmap's "done when").

```powershell
git add loadtest docker-compose.yml .gitignore README.md ROADMAP.md docs/PHASE_7_GUIDE.md
git commit -m "Phase 7: Locust load tests, results in the README"
git push
```

**Check your understanding:**

1. What do throughput, latency and percentiles each tell you?
2. Why are p50 and p95 better than the average response time?
3. Why does each simulated client get its own API key?
4. Why are 429s recorded as successes, and how are they kept apart from allowed requests?
5. What did you expect *before* running, and why does that matter?
6. Why did polite clients first look 8 times slower than greedy ones, and what fixed it?
7. What did `docker compose run` do without `--no-deps`, and how was it noticed?
8. How do you tell whether the system or the load generator is the bottleneck?
9. Why does the sliding window counter allow fewer requests per second than the log?
10. What is the bottleneck in RateGate's stack, and what does that mean for scaling?
11. Why did a single copy produce `502`s under full load, but three copies didn't?
12. Why is it essential to state the machine and conditions next to load-test numbers?
