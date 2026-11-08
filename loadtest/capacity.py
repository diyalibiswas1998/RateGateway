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
