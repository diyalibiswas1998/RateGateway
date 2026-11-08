import socket

import httpx

from app.config import Settings, settings
from app.limiter.token_bucket import TokenBucket
from app.main import app


async def get(path, limiter, api_key, times=1):
    """Send requests through the real app, the way nginx would hand them over."""
    app.state.limiters = {"free": limiter, "paid": limiter}
    app.state.paid_api_keys = set()
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
