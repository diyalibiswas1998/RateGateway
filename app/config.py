import socket
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Algorithm = Literal["token_bucket", "sliding_log", "sliding_counter"]
FailMode = Literal["open", "closed"]
Tier = Literal["free", "paid"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RATEGATE_", env_file=".env")

    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50  # connections shared by all requests
    redis_pool_timeout: float = 5.0  # seconds to wait for a free connection
    redis_connect_timeout: float = 1.0  # seconds to open a connection
    redis_socket_timeout: float = 0.25  # seconds to wait for a command's reply
    fail_mode: FailMode = "open"  # what to do when Redis can't be reached
    instance_id: str = Field(default_factory=socket.gethostname)  # names this copy
    algorithm: Algorithm = "token_bucket"

    # Token bucket
    capacity: int = 10  # max tokens in a bucket (burst size)
    refill_rate: float = 1.0  # tokens added per second

    # Sliding window log and sliding window counter
    limit: int = 10  # max requests per window
    window_seconds: float = 10.0  # window length

    # Paid tier: these API keys get the paid_* limits instead (10 times the free ones).
    # From the environment as JSON, e.g. RATEGATE_PAID_API_KEYS='["key-1", "key-2"]'
    paid_api_keys: set[str] = set()
    paid_capacity: int = 100
    paid_refill_rate: float = 10.0
    paid_limit: int = 100
    paid_window_seconds: float = 10.0


settings = Settings()
