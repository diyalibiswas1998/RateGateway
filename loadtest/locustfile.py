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
