"""Turn Locust's CSV results into a Markdown table, one row per run.

Usage: uv run python loadtest/summarize.py "Token bucket=loadtest/results/token_bucket" ...
Each argument is a label and the --csv prefix Locust was given.
"""

import argparse
import csv


def load(prefix: str) -> dict[str, dict[str, str]]:
    with open(f"{prefix}_stats.csv", newline="") as f:
        return {row["Name"]: row for row in csv.DictReader(f)}


def count(rows: dict, name: str) -> int:
    return int(rows[name]["Request Count"]) if name in rows else 0


def rate(rows: dict, name: str) -> float:
    return float(rows[name]["Requests/s"]) if name in rows else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", help="LABEL=CSV_PREFIX")
    parser.add_argument("--greedy-clients", type=int, default=10)
    args = parser.parse_args()

    print(
        "| Algorithm | Requests/s | p50 | p95 | Limited (429) "
        "| Polite clients limited | Allowed per greedy client |"
    )
    print("|---|---|---|---|---|---|---|")
    for run in args.runs:
        label, prefix = run.split("=", 1)
        rows = load(prefix)
        total = rows["Aggregated"]
        limited = count(rows, "polite limited") + count(rows, "greedy limited")
        answered = (
            limited + count(rows, "polite allowed") + count(rows, "greedy allowed")
        )
        polite = count(rows, "polite allowed") + count(rows, "polite limited")
        greedy_rate = rate(rows, "greedy allowed") / args.greedy_clients
        print(
            f"| {label} | {float(total['Requests/s']):.0f} "
            f"| {total['50%']} ms | {total['95%']} ms "
            f"| {100 * limited / answered:.1f}% "
            f"| {count(rows, 'polite limited')} of {polite} "
            f"| {greedy_rate:.2f}/s |"
        )


if __name__ == "__main__":
    main()
