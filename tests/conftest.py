from uuid import uuid4

import pytest

from app.config import settings
from app.redis_client import create_redis


@pytest.fixture
async def redis():
    client = create_redis(settings)
    yield client
    await client.aclose()


@pytest.fixture
def client_id():
    # A fresh id per test, so tests never share a bucket or wait for old keys to expire.
    return f"test-{uuid4()}"
