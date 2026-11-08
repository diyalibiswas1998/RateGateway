"""Read /metrics from every API copy and add up the rate-limit decisions.

Each copy counts only the requests it answered, and /metrics isn't reachable through
nginx, so this asks each container directly, the way a monitoring system would.
"""

import subprocess
from collections import Counter

from prometheus_client.parser import text_string_to_metric_families

# Runs inside a container: fetch that copy's own /metrics.
FETCH = (
    "import urllib.request; "
    "print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode())"
)


def api_containers() -> list[str]:
    result = subprocess.run(
        ["docker", "compose", "ps", "api", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(result.stdout.split())


def decisions(container: str) -> Counter:
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", FETCH],
        capture_output=True,
        text=True,
        check=True,
    )
    counts = Counter()
    for family in text_string_to_metric_families(result.stdout):
        if family.name == "rategate_decisions":
            for sample in family.samples:
                if sample.name == "rategate_decisions_total":
                    key = (sample.labels["tier"], sample.labels["outcome"])
                    counts[key] += int(sample.value)
    return counts


def main() -> None:
    total = Counter()
    for container in api_containers():
        counts = decisions(container)
        total += counts
        summary = ", ".join(f"{t} {o}: {n}" for (t, o), n in sorted(counts.items()))
        print(f"{container}  {summary or 'nothing yet'}")
    summary = ", ".join(f"{t} {o}: {n}" for (t, o), n in sorted(total.items()))
    print(f"All copies  {summary or 'nothing yet'}")


if __name__ == "__main__":
    main()
