"""Send requests one at a time and show which server copy answered each, and how."""

import argparse
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/resource")
    parser.add_argument("--total", type=int, default=14)
    parser.add_argument("--api-key", default=f"seq-{uuid4().hex[:8]}")
    args = parser.parse_args()

    print(f"API key {args.api_key}")
    with httpx.Client(timeout=10) as client:
        for n in range(1, args.total + 1):
            response = client.get(args.url, headers={"X-API-Key": args.api_key})
            instance = response.headers.get("X-Instance-Id", "(no header)")
            remaining = response.headers.get("X-RateLimit-Remaining", "-")
            print(f"{n:3}  {response.status_code}  {instance}  remaining={remaining}")


if __name__ == "__main__":
    main()
