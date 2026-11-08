# Phase 6 Guide — Continuous Integration with GitHub Actions

**Goal of this phase:** every push to GitHub is checked automatically — lint, formatting, all the tests
against a real Redis, and the full Docker stack — and the result shows as a green check or a red cross next
to the commit, plus a badge in the README.

This proves the resume line *"GitHub Actions CI"*.

**What we measured** (2026-11-02, on GitHub's machines):

| Result | Evidence |
|---|---|
| The workflow passes | 4 green runs out of 4 on a trial branch |
| It's quick | Lint and test: 29–91 s per run (the tests themselves: 17 s); Docker stack: 32–50 s; both jobs run at the same time |
| It catches a broken test | A deliberately wrong assertion turned the run red at "Run tests", exit code 1 |
| It checks the real timeouts | CI has no `.env`, so all 40 tests pass with the production 0.25 s / 1 s |
| It checks Phase 5's claim on every push | 200 requests through nginx, limit 100: exactly 100 allowed, across 3 copies |

The very first attempt failed in 4 seconds because of a version tag that doesn't exist (Concept 8); after
that, everything went as planned.

---

## Part A — Concepts (read before coding)

### 1. What CI is, and why

**Continuous integration (CI)** means: every time code is pushed, a fresh machine downloads it, installs
everything from scratch, and runs the checks. If anything fails, everyone sees it at once.

Until now, the checks only ran when you remembered to run them, on your machine, with whatever happened to
be installed there. CI fixes three problems:

| Without CI | With CI |
|---|---|
| "I forgot to run the tests before pushing" | Every push is checked, always |
| "Works on my machine" — because of something only your machine has | A clean machine every time: if a file, a dependency or a setting is missing from git, CI fails |
| Nobody can see whether `main` works | A green check or red cross on every commit, and a badge in the README |

For a resume project, the badge also tells a reviewer at a glance that the tests exist and pass.

### 2. GitHub Actions: the vocabulary

**GitHub Actions** is GitHub's built-in CI service, free for public repositories. It's driven by **workflow**
files in your repository, in the folder `.github/workflows/`. GitHub finds them automatically.

| Word | Meaning | In RateGate |
|---|---|---|
| **Workflow** | One YAML file describing what to run and when | `.github/workflows/ci.yml`, named "CI" |
| **Trigger** (`on:`) | The events that start it | Every `push` (any branch) and every `pull_request` |
| **Run** | One execution of the workflow, for one commit | One per push |
| **Job** | A group of steps that runs on its own fresh machine; jobs run **in parallel** | `test` and `stack` |
| **Runner** | The machine a job runs on, created for the job and thrown away after | `ubuntu-latest`: a Linux virtual machine GitHub provides |
| **Step** | One command (`run:`) or one ready-made action (`uses:`), in order | Install uv, lint, test … |
| **Action** | A reusable step someone published, referenced as `owner/name@version` | `actions/checkout@v7`, `astral-sh/setup-uv@v10.3.3` |
| **Service container** | A container GitHub starts next to a job, for the job to use | Redis 7 |

A job **passes** when every step exits with code 0. The first step that fails stops the job (later steps are
skipped, unless they say otherwise), and the run turns red.

### 3. A fresh machine has nothing

The runner starts as a clean Ubuntu with common tools (git, Docker, curl) but **none of your project's
things**. So the workflow must do, in order, what you did by hand in Phase 0:

1. **Get the code** — `actions/checkout` clones the commit being checked.
2. **Install uv** — `astral-sh/setup-uv`, pinned to `0.13.6`, the version on your PC.
3. **Install Python and the libraries** — `uv sync --locked`. uv reads `.python-version` (3.12), downloads that
   Python, and installs exactly the versions in `uv.lock`. `--locked` makes it **fail** if `uv.lock` doesn't
   match `pyproject.toml` — the classic "I added a library but forgot to commit the lock file" mistake.
4. **Start Redis** — a service container (Concept 4).
5. **Run the checks** — the same commands you run locally.

**Nothing from your PC is there:** not `.venv`, not your Phase 0 Redis, and not your `.env`. That last one
matters: in Phase 5 we gave your PC looser timeouts because Docker Desktop on Windows is slow. CI has no
`.env`, so it tests the **real** production timeouts (0.25 s reply, 1 s connect) — exactly what we wanted.

### 4. Service containers

Our tests need a real Redis on `localhost:6379`. In the workflow:

```yaml
    services:
      redis:
        image: redis:7
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 2s
          --health-timeout 3s
          --health-retries 15
```

- GitHub starts a `redis:7` container **before** the job's steps, and removes it after.
- `ports: - 6379:6379` publishes it on the runner's port 6379, so the tests' default `redis://localhost:6379/0`
  works unchanged — no CI-specific settings.
- `options:` are extra flags for `docker run`. The four `--health-*` flags give it a health check, and GitHub
  **waits until it's healthy** before starting the steps. Without that, the first tests could start before
  Redis accepts connections.
- `>-` is YAML for "fold the following lines into one line" — the options are really one long string.

### 5. Two jobs, in parallel

| Job | What it proves | Why separate |
|---|---|---|
| **Lint and test** | Code style is clean; all tests pass against a real Redis | The fast, essential check |
| **Docker stack** | The Phase 5 system builds and works: 3 copies behind nginx, one shared limit, exactly | Needs Docker Compose, not a service container; runs at the same time on its own machine |

The roadmap only asked for the first. The second turns Phase 5's headline claim — *the limit holds across
copies* — into something checked on every push, using the burst script with two new options:
`--expect-allowed 100 --expect-instances 3` make it **exit with an error** if the numbers are different. A
script that only prints results can't fail a CI job; one that checks them can.

### 6. Pinning versions

Every version in the workflow is fixed: `ubuntu-latest` aside, the actions (`@v7`, `@v10.3.3`), uv (`0.13.6`),
Python (via `.python-version`), every library (via `uv.lock`), and the images (`redis:7`, `nginx:1.27-alpine`,
`python:3.12-slim`). If CI turns red, it should be because **your** change broke something — not because a
tool released a new version overnight.

`@v7` means "the newest v7.x.y": it gets bug fixes but no breaking changes, which is how actions are usually
referenced — when the publisher provides such a tag (Concept 8).

### 7. Reading a red run

When a run fails, GitHub shows a red cross on the commit and (by default) emails you. Click it → the failed
job → the failed step, which is expanded with its full output: for pytest, the same failure report you see
locally. The workflow also has a step that runs only on failure (`if: failure()`) and prints the containers'
logs, so a broken stack explains itself.

### 8. Version tags: what we got wrong first

Actions are referenced by a **git tag** in their repository. Most publishers keep a short "major" tag such as
`v7` that always points at the newest `v7.x.y`. `actions/checkout@v7` works that way.

The first version of our workflow used `astral-sh/setup-uv@v10` by analogy. Both jobs failed within 4 seconds,
at **"Set up job"**, before running a single step of ours. The run's error note said:

```
Unable to resolve action `astral-sh/setup-uv@v10`, unable to find version `v10`
```

Listing setup-uv's tags (GitHub's API: `https://api.github.com/repos/astral-sh/setup-uv/tags`) showed why: it
has short tags only up to `v7`; from `v8` on it publishes **full versions only** (`v10.3.3`, `v10.3.2`, …).
So the workflow pins `astral-sh/setup-uv@v10.3.3`. Lesson, once more: check that a version exists instead
of assuming it follows the same pattern as another tool's.

**"Set up job" failures** mean GitHub couldn't even prepare the job: a misspelled or missing action, a bad
version, a YAML error. Failures **inside** a step (red step, e.g. "Run tests") mean the job ran and a command
failed.

---

## Part B — Setup

Nothing to install. You need:

1. Your repository on GitHub (since Phase 0) — public, so Actions is free with no limits that matter here.
2. Actions enabled — it is by default. Check: your repository on github.com → the **Actions** tab shows
   workflows, not a "disabled" notice.
3. Docker Desktop and your Phase 0 Redis running, to run the checks locally once before pushing.

---

## Part C — Build it (in this order)

Files touched (★ new, ✎ changed):

```
.github/
  workflows/
    ci.yml                     ★ Step 2
scripts/concurrent_burst.py    ✎ Step 1 (--expect-allowed, --expect-instances)
README.md                      ✎ Step 5 (badge, CI paragraph)
ROADMAP.md                     ✎ Step 7
```

| Step | What | Why here |
|---|---|---|
| 1 | Give the burst script pass/fail options | CI can only fail on a non-zero exit code; a script that just prints can't |
| 2 | Write `.github/workflows/ci.yml` | The workflow itself |
| 3 | Run every check locally first | A red CI run costs minutes; a local run costs seconds |
| 4 | Commit, push, watch the run | The real test of the workflow |
| 5 | Badge and CI paragraph in the README | Show the result on the project's front page |
| 6 | Prove it can fail: a broken test on a temporary branch | A check that can't fail proves nothing (Phase 4's lesson) |
| 7 | ROADMAP | Record it |

### Step 1 — Pass/fail options for the burst script

In `scripts/concurrent_burst.py`, two new options after `--api-key`:

```python
    parser.add_argument("--api-key", default=f"burst-{uuid4().hex[:8]}")
    # For automated checks (CI): exit with an error if the result is different.
    parser.add_argument("--expect-allowed", type=int, help="exact number of 200s")
    parser.add_argument("--expect-instances", type=int, help="number of copies")
    args = parser.parse_args()
```

and at the end of `main()`, after the per-instance printout:

```python
    problems = []
    if args.expect_allowed is not None and counts[200] != args.expect_allowed:
        problems.append(f"expected {args.expect_allowed} allowed, got {counts[200]}")
    if args.expect_instances is not None and len(by_instance) != args.expect_instances:
        problems.append(
            f"expected {args.expect_instances} instances, got {len(by_instance)}"
        )
    if problems:
        raise SystemExit("FAILED: " + "; ".join(problems))
```

**What's happening:**

- **Optional options.** Without `--expect-...`, the script behaves exactly as before (`args.expect_allowed` is
  `None`, so nothing is checked). With them, it compares the real numbers to the expected ones.
- **`counts[200]`** — a `Counter` returns `0` for a key it has never seen, so this works even if nothing was allowed.
- **`len(by_instance)`** — how many different `X-Instance-Id` values answered.
- **`raise SystemExit("FAILED: ...")`** — ends the program, prints the message, and sets the **exit code** to 1.
  Every program returns an exit code when it finishes: 0 means success, anything else means failure. That number
  is what CI looks at.
- **All problems are collected first**, then reported together, so one run tells you everything that's wrong.

**Try both directions** with the stack running at limit 100:

```powershell
$env:RATEGATE_CAPACITY = "100"; $env:RATEGATE_REFILL_RATE = "0.001"
docker compose up --build -d --wait
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200 --expect-allowed 100 --expect-instances 3
echo "exit code: $LASTEXITCODE"
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200 --expect-allowed 99 --expect-instances 4
echo "exit code: $LASTEXITCODE"
Remove-Item Env:RATEGATE_*
docker compose down
```

`$LASTEXITCODE` is PowerShell's variable holding the last program's exit code. Ours:

```
  200: 100
  429: 100
...
exit code: 0
...
FAILED: expected 99 allowed, got 100; expected 4 instances, got 3
exit code: 1
```

### Step 2 — `.github/workflows/ci.yml`

Create the folders `.github` and `.github/workflows` (the leading dot matters), then `ci.yml` inside:

```yaml
# Runs on every push and pull request. Both jobs must pass for a green check.
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    name: Lint and test
    runs-on: ubuntu-latest
    timeout-minutes: 10

    # A real Redis for the tests, reachable at localhost:6379 like on your machine.
    services:
      redis:
        image: redis:7
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 2s
          --health-timeout 3s
          --health-retries 15

    steps:
      - uses: actions/checkout@v7

      # The same uv version as on the development machine.
      - uses: astral-sh/setup-uv@v10.3.3
        with:
          version: "0.13.6"

      # --locked: fail if uv.lock is out of date with pyproject.toml.
      - name: Install dependencies
        run: uv sync --locked

      - name: Lint
        run: uv run ruff check .

      - name: Check formatting
        run: uv run ruff format --check .

      - name: Run tests
        run: uv run pytest -v

  stack:
    name: Docker stack (3 copies behind nginx)
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - uses: actions/checkout@v7

      - uses: astral-sh/setup-uv@v10.3.3
        with:
          version: "0.13.6"

      # Only the script's dependency (httpx); the API itself runs in the containers.
      - name: Install dependencies
        run: uv sync --locked

      # Limit 100 with nothing refilling, so the expected result is exact.
      - name: Start the stack
        env:
          RATEGATE_CAPACITY: "100"
          RATEGATE_REFILL_RATE: "0.001"
        run: docker compose up --build -d --wait

      - name: Burst through nginx stays exactly at the limit
        run: >-
          uv run python scripts/concurrent_burst.py
          --url http://127.0.0.1:8080/api/resource --total 200
          --expect-allowed 100 --expect-instances 3

      - name: Show container logs
        if: failure()
        run: docker compose logs

      - name: Stop the stack
        if: always()
        run: docker compose down
```

**Piece by piece** (YAML: indentation with spaces means "belongs to"; `- ` starts a list item):

- **`name: CI`** — the workflow's name, shown in the Actions tab and used by the badge.
- **`on: push:` / `pull_request:`** — run on every push to any branch, and on every pull request. (A key with
  nothing after it means "with default settings".)
- **`jobs:`** — the two jobs, `test` and `stack`. Nothing links them, so GitHub runs them **at the same time**
  on two separate machines.
- **`name: Lint and test`** — the job's display name, shown next to the commit.
- **`runs-on: ubuntu-latest`** — a fresh Linux virtual machine. (GitHub currently notes that this label is
  moving to Ubuntu 26; nothing in our workflow depends on the Ubuntu version.)
- **`timeout-minutes: 10`** — stop the job if it runs longer. The default is 6 hours; a hung test shouldn't
  run that long. Our jobs take under 2 minutes.
- **`services: redis:`** — Concept 4.
- **`steps:`** — in order, each either `uses:` an action or `run:`s a shell command.
- **`uses: actions/checkout@v7`** — clone the commit being checked onto the runner.
- **`uses: astral-sh/setup-uv@v10.3.3` with `version: "0.13.6"`** — install uv, the same version as yours.
  `with:` passes inputs to an action; the version is quoted so YAML reads it as text, not a number. The action
  also caches downloaded packages between runs (its `enable-cache` input defaults to `auto`).
- **`name: Install dependencies` / `run: uv sync --locked`** — Concept 3. `name:` is only the label shown on
  GitHub; `run:` is the command.
- **Lint, Check formatting, Run tests** — exactly the commands you run locally. `-v` makes pytest list every
  test, so the log shows which one failed.
- **The `stack` job** has no service container: Compose starts its own Redis.
- **`env:` on "Start the stack"** — environment variables for that one step. Compose passes them into the API
  copies (the pass-through list in `docker-compose.yml`, Phase 5): limit 100, no refill, so the expected
  answer is exactly 100.
- **`run: >-` with the command over three lines** — `>-` folds them into one command line; it's just for
  readability.
- **`if: failure()`** — run this step **only** if an earlier step failed: print every container's log, so a red
  run explains itself.
- **`if: always()`** — run this step whatever happened before, so the stack is always stopped. (The runner is
  thrown away anyway; it's a good habit for machines that aren't.)

### Step 3 — Run every check locally first

Before pushing, run what CI will run, so a typo costs seconds instead of a CI round trip:

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
$env:RATEGATE_CAPACITY = "100"; $env:RATEGATE_REFILL_RATE = "0.001"
docker compose up --build -d --wait
uv run python scripts/concurrent_burst.py --url http://127.0.0.1:8080/api/resource --total 200 --expect-allowed 100 --expect-instances 3
Remove-Item Env:RATEGATE_*
docker compose down
```

Expected: nothing to change from `uv sync --locked`, `All checks passed!`, `20 files already formatted`,
`40 passed`, then the burst ending with exit code 0.

Your PC uses its `.env` timeouts for the tests (Phase 5); CI won't. That's intended: CI checks the real values.

### Step 4 — Commit, push, watch

```powershell
git add .github scripts/concurrent_burst.py
git commit -m "Phase 6: GitHub Actions CI"
git push
```

Then on github.com: your repository → **Actions** tab. A run named after your commit message appears within
seconds: yellow dot (running), then green check (passed) or red cross (failed). Click it to see both jobs;
click a job to see each step and its output.

**Our first attempt** failed both jobs at "Set up job" within 4 seconds (Concept 8): `unable to find version
v10`. After pinning `v10.3.3`, both jobs passed. Times from that run:

| Job | Total | Slowest step |
|---|---|---|
| Lint and test | 53 s | Run tests: 17 s; starting the Redis service: 5 s |
| Docker stack | 50 s | Start the stack (build the image, start 5 containers): 19 s; the burst: 2 s |

The tests take 17 s here against about 27 s on your PC: on Linux a closed port refuses a connection instantly,
so the outage tests don't wait for Windows' 2-second refusal.

### Step 5 — The badge

At the very top of `README.md`, under the title:

```markdown
[![CI](https://github.com/diyalibiswas1998/RateGate/actions/workflows/ci.yml/badge.svg)](https://github.com/diyalibiswas1998/RateGate/actions/workflows/ci.yml)
```

- **`![CI](...badge.svg)`** — an image. GitHub draws it live: "CI passing" in green or "CI failing" in red, for
  the latest run on the default branch (`main`).
- **The outer `[...](...)`** makes the image a link to the workflow's runs.

And in the Testing section, a sentence saying what CI checks. Commit and push; the badge appears on the
repository's front page.

### Step 6 — Prove it can fail

A check that has never failed might not be able to. Break one test on a **temporary branch**, so `main`
never gets a red commit:

```powershell
git switch -c ci-red-check
```

In `tests/test_instance_id.py`, change the last line's `"api-7"` on the right to `"api-8"`:

```python
    assert Settings(instance_id="api-7").instance_id == "api-8"
```

```powershell
git commit -am "Deliberately broken test: CI must turn red"
git push -u origin ci-red-check
```

- **`git switch -c ci-red-check`** — create a new branch and switch to it.
- **`git push -u origin ci-red-check`** — send it to GitHub; `-u` remembers where it goes.

**What we saw:** the run turned **red**. "Lint and test" failed at **Run tests** (exit code 1; the steps after
it were skipped), while "Docker stack" still passed — the jobs are independent. The failing step's log shows
the same report as locally:

```
>       assert Settings(instance_id="api-7").instance_id == "api-8"
E       AssertionError: assert 'api-7' == 'api-8'
FAILED tests/test_instance_id.py::test_instance_id_defaults_to_the_hostname
```

Then clean up — back to `main`, and delete the branch locally and on GitHub:

```powershell
git switch main
git branch -D ci-red-check
git push origin --delete ci-red-check
```

The red run stays in the Actions history — that's fine, it's evidence the check works. `main` and the badge are
untouched, because the badge only reflects `main`.

### Step 7 — ROADMAP

Tick the three Phase 6 items and add a progress-log row.

---

## Part D — Verify

| Check | How | Expected |
|---|---|---|
| 1. Green on `main` | Actions tab, or the ✓ next to the latest commit on the repository page | Both jobs green |
| 2. Badge | The repository's front page | "CI passing", green |
| 3. Stable | Our trial: 4 runs in a row | 4 green |
| 4. Can fail | Step 6 | Red at "Run tests", one `FAILED` line |
| 5. Real timeouts | The "Run tests" log | `40 passed`, with no `.env` involved |
| 6. Phase 5's claim | The "Burst through nginx…" log | `200: 100`, `429: 100`, three instance IDs, step green |

---

## Part E — Common pitfalls

| Pitfall | Symptom | Why | Fix |
|---|---|---|---|
| A version tag that doesn't exist | Job fails at "Set up job" in seconds: `unable to find version` | Not every action publishes short major tags | Check its tags; use one that exists (`v10.3.3`) |
| Workflow file in the wrong place | Nothing happens on push | GitHub only reads `.github/workflows/*.yml` | Exact path, leading dot included |
| Tabs in YAML | `Invalid workflow file` | YAML only allows spaces for indentation | Spaces only |
| No health check on the service | Early tests fail with connection errors, sometimes | Steps start before Redis is ready | The `--health-*` options |
| Not committing `uv.lock` | `uv sync --locked` fails in CI | CI only has what's in git | Commit lock-file changes together with `pyproject.toml` |
| Relying on something only your PC has | Green locally, red in CI | `.env`, a global tool, an untracked file | That's what CI is for: add what's missing to git or to the workflow |
| A check script that only prints | CI stays green even when the numbers are wrong | CI only sees exit codes | `--expect-...` options with `SystemExit` |
| Breaking `main` to test CI | The badge turns red for everyone | The badge reflects `main` | A temporary branch (Step 6) |
| No `timeout-minutes` | A hang runs for up to 6 hours | That's the default | `timeout-minutes: 10` |
| Re-running a red job until it's green | A real problem stays hidden | Failure treated as noise | Read the failing step's log first (Phase 5's lesson) |

---

## Part F — Finish the phase

**Done when:** a pushed commit shows a green check on GitHub, and a deliberately broken test turns it red (the
roadmap's "done when").

```powershell
git add README.md ROADMAP.md docs/PHASE_6_GUIDE.md
git commit -m "Phase 6: CI badge, README and roadmap"
git push
```

**Check your understanding:**

1. Which problems does CI solve that running the tests locally doesn't?
2. What's the difference between a workflow, a run, a job and a step?
3. Why must the workflow install uv, Python and the libraries itself?
4. What does `--locked` add to `uv sync`, and which mistake does it catch?
5. What is a service container, and why does ours need a health check?
6. Why do the two jobs run at the same time, and why is the stack a separate job?
7. How can a Python script make a CI step fail?
8. Why did the first run fail at "Set up job", and what does a failure there usually mean?
9. Why does CI test the production timeouts while your PC uses looser ones?
10. Why was the broken test pushed to a temporary branch, not `main`?
11. What do `if: failure()` and `if: always()` do?


