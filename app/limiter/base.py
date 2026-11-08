from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent / "scripts"


def load_script(name: str) -> str:
    return (SCRIPTS_DIR / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # seconds until the next request would be allowed (0 if allowed)
    reset_after: float  # seconds until the client's full allowance is back

    @classmethod
    def from_script(cls, result: list[int], limit: int) -> "Decision":
        allowed, remaining, retry_ms, reset_ms = result
        return cls(
            allowed=bool(allowed),
            limit=limit,
            remaining=int(remaining),
            retry_after=retry_ms / 1000,
            reset_after=reset_ms / 1000,
        )


class RateLimitStrategy(ABC):
    @abstractmethod
    async def check(self, client_id: str, cost: int = 1) -> Decision:
        """Spend `cost` from the client's allowance if there is enough, and report the result."""
