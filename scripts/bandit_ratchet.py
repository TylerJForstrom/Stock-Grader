#!/usr/bin/env python3
"""Fail CI when the low-severity bandit count grows.

Low-severity findings are not release blockers, so the scan that reports them
is advisory. Advisory used to mean the step failed on every run and therefore
told nobody anything. This ratchet keeps it honest instead: the current
findings are recorded per rule in ``bandit-low-baseline.json`` beside this
file, and CI fails if any rule's count INCREASES.

Per rule, not just the total, so that fixing one assert while adding a
subprocess call cannot slip through on an unchanged total.

Dropping below the baseline is not a failure — it prints the new numbers and
asks for the baseline to be lowered, which is how the ratchet tightens. Run
with ``--update`` to rewrite the baseline after deliberately resolving
findings.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path

BASELINE_PATH = Path(__file__).resolve().parent / "bandit-low-baseline.json"
SCAN_TARGETS = ("src", "scripts")
REPO_ROOT = Path(__file__).resolve().parent.parent


def current_counts() -> Counter[str]:
    """Low-severity findings per bandit test id, from a fresh scan."""
    # Safe by construction: argv is a literal list, shell=False, and every
    # element is a constant in this file, so nothing here is reachable from
    # user input.
    completed = subprocess.run(  # nosec B603
        [sys.executable, "-m", "bandit", "-q", "-r", *SCAN_TARGETS, "-f", "json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if not completed.stdout.strip():
        raise SystemExit(f"bandit produced no JSON output:\n{completed.stderr}")
    payload = json.loads(completed.stdout)
    return Counter(
        result["test_id"]
        for result in payload["results"]
        if result["issue_severity"].upper() == "LOW"
    )


def load_baseline() -> Counter[str]:
    data = json.loads(BASELINE_PATH.read_text())
    return Counter(data["per_rule"])


def write_baseline(counts: Counter[str]) -> None:
    payload = {
        "_comment": (
            "Low-severity bandit findings per rule. CI fails if any count rises. "
            "Lower these as findings are resolved; never raise one to make CI pass."
        ),
        "total": sum(counts.values()),
        "per_rule": dict(sorted(counts.items())),
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the baseline from the current scan (only to record a REDUCTION)",
    )
    args = parser.parse_args()

    counts = current_counts()
    if args.update:
        write_baseline(counts)
        print(f"baseline updated: {sum(counts.values())} low-severity finding(s)")
        return 0

    baseline = load_baseline()
    regressions = sorted(
        (rule, baseline.get(rule, 0), counts[rule])
        for rule in counts
        if counts[rule] > baseline.get(rule, 0)
    )
    if regressions:
        print("Low-severity bandit findings INCREASED:")
        for rule, was, now in regressions:
            print(f"  {rule}: {was} -> {now}")
        print(
            "\nFix the new finding, or justify it in place with a narrow "
            "'# nosec <rule>' comment saying why it is safe."
        )
        return 1

    improvements = sorted(
        (rule, baseline[rule], counts.get(rule, 0))
        for rule in baseline
        if counts.get(rule, 0) < baseline[rule]
    )
    if improvements:
        print("Low-severity bandit findings decreased — tighten the ratchet:")
        for rule, was, now in improvements:
            print(f"  {rule}: {was} -> {now}")
        print(f"\nRun: python {Path(__file__).name} --update")
        return 1

    print(f"low-severity bandit findings unchanged at {sum(counts.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
