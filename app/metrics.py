from prometheus_client import Counter, Histogram

# Each API copy keeps its own counts; a monitoring system reads every copy's /metrics
# and adds them up.
DECISIONS = Counter(
    "rategate_decisions",  # published as rategate_decisions_total
    "Rate-limit decisions, by outcome (allowed, limited, fail_open, fail_closed)",
    ["outcome", "tier"],
)

CHECK_SECONDS = Histogram(
    "rategate_check_seconds",
    "Time spent asking Redis whether a request may go ahead, in seconds",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
