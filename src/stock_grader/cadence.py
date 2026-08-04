"""Expectation clocks for the monthly evidence loop.

The forward-evidence loop has two monthly clocks — monthly-freeze (the 1st)
and monthly-forward-backtest (the 6th) — and before this module neither had a
cadence watcher: a silently disabled workflow left exactly the same trace as a
healthy one in months where nothing matured. These checks are
*expectation-based*, not age-based (the same distinction Stock-Vault's
``check_market_eod`` draws): by a jitter-tolerant day of the month, the
month's artifact must exist, or the check fails and the red run is the alarm.

Two clocks:

* **accounting** — ``docs/forward/<YYYY-MM>/accounting.json`` is written
  unconditionally by every monthly-forward-backtest run, recording every
  profile's evaluated / not-matured / refused state.  "The loop ran and
  nothing matured" is a recorded fact, not silence (attest false over
  optimistic true).  By day ``ACCOUNTING_GRACE_DAY`` the current month's
  artifact must exist; before that, the previous month's must.
* **freeze** — the newest dated panel under ``frozen_scores/<profile>/``
  must be from the current month once day ``FREEZE_GRACE_DAY`` is reached.
  This closes monthly-freeze's documented lack of a staleness gate with a
  lag of days instead of the ~45-day paper-trader interlock backstop.

Both clocks are bootstrap-guarded: no artifacts at all is a pass with a note,
so the checks can land before the first accounting artifact exists.

Cross-repo contract (pinned by ``tests/test_cadence.py``): this module is
**stdlib-only** and runs as a bare script with no package install —
Stock-Vault's shadow-arms workflow executes

    python grader/src/stock_grader/cadence.py check --repo-root grader

directly from its Stock-Grader checkout, so importing anything outside the
standard library, or importing relatively, would break the cross-coverage leg
in a sibling repo's CI.
"""

from __future__ import annotations

import os
import sys

# Bare-script execution puts THIS file's directory — the stock_grader package,
# whose types.py shadows the stdlib `types` module — at sys.path[0], and the
# very next stdlib import would resolve the wrong one ("cannot import name
# 'GenericAlias' from partially initialized module 'types'"). Drop it before
# importing anything else. `os` and `sys` are safe: both are already loaded
# during interpreter startup. Package imports never enter this branch.
if __name__ == "__main__":
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]

import argparse
import datetime as dt
import json
import re
from collections.abc import Sequence
from pathlib import Path

# monthly-freeze runs on the 1st at 13:19 UTC.  Three days of slack covers
# GitHub's best-effort cron (5-60+ min jitter, occasional whole-run skips)
# plus a manual re-dispatch window before the check goes red.
FREEZE_GRACE_DAY = 4

# monthly-forward-backtest runs on the 6th at 02:41 UTC; two days of slack for
# the same reasons.
ACCOUNTING_GRACE_DAY = 8

ACCOUNTING_FILENAME = "accounting.json"
ACCOUNTING_SCHEMA_VERSION = "1.0"

# EVERY committed forward-evidence root monthly-freeze writes, not just the
# first one.  monthly-freeze.yml runs `freeze --out frozen_scores` AND `freeze
# --out frozen_scores_wide` on the same cron and commits both; docs/UNIVERSE.md
# calls them "two deliberately separate forward records".  A clock that watched
# only one left half the evidence with exactly the property this module exists
# to eliminate: a stalled month leaving the same trace as a healthy one.  A
# root that does not exist (a wheel install, a partial checkout) is
# bootstrap-guarded per root, so adding one can never fail a tree that has none.
FROZEN_ROOTS = ("frozen_scores", "frozen_scores_wide")

# The controlled vocabulary for a profile's monthly state.  Deliberately a
# closed set of bare labels: refusal DETAIL stays in the workflow log, so the
# public accounting artifact can never carry a licensed derived number no
# matter what a refusal message interpolates.
VALID_STATES = (
    "evaluated",  # built, ready_for_backtest, declared, and backtested
    "not_matured",  # built cleanly; no forward window has completed yet
    "refused_build",  # build-panel refused or failed
    "refused_declare",  # ledger-declare refused or failed
    "refused_backtest",  # backtest refused or failed
)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATED_PANEL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.parquet$")


class CadenceError(Exception):
    """A refusal: bad states, a corrupt artifact, or a malformed month."""


def _month_of(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{mon - 1:02d}"


def expected_month(today: dt.date, grace_day: int) -> str:
    """The oldest month that satisfies a monthly clock on ``today``.

    Before ``grace_day`` the month's scheduled run may legitimately not have
    happened yet, so the previous month suffices; from ``grace_day`` on, the
    current month is due.  ``YYYY-MM`` strings compare correctly as text.
    """
    if today.day >= grace_day:
        return _month_of(today)
    return previous_month(_month_of(today))


def newest_accounting_month(forward_dir: Path) -> str | None:
    """Newest ``<YYYY-MM>`` under ``forward_dir`` that contains accounting.json.

    A month directory holding only backtest reports does not count: the
    accounting artifact is the loop's heartbeat, and only its presence
    proves the loop ran.
    """
    if not forward_dir.is_dir():
        return None
    months = [
        entry.name
        for entry in forward_dir.iterdir()
        if entry.is_dir()
        and _MONTH_RE.match(entry.name)
        and (entry / ACCOUNTING_FILENAME).is_file()
    ]
    return max(months, default=None)


def newest_freeze_month(frozen_root: Path) -> str | None:
    """Month of the newest ``<profile>/<YYYY-MM-DD>.parquet`` freeze.

    Derived from artifact filenames, never mtimes — checkout mtimes describe
    the checkout, not the observation.  Non-dated files (manifest.json,
    catalogs) are ignored.
    """
    if not frozen_root.is_dir():
        return None
    months: list[str] = []
    for profile_dir in frozen_root.iterdir():
        if not profile_dir.is_dir():
            continue
        months.extend(
            artifact.name[:7]
            for artifact in profile_dir.iterdir()
            if _DATED_PANEL_RE.match(artifact.name)
        )
    return max(months, default=None)


def check_cadence(
    repo_root: Path | str,
    today: dt.date,
    *,
    pre_run: bool = False,
    frozen_roots: Sequence[str] = FROZEN_ROOTS,
    forward_dir: str = "docs/forward",
) -> tuple[bool, list[str]]:
    """Check both expectation clocks; returns ``(ok, report_lines)``.

    ``pre_run`` is for the self-gate at the top of monthly-forward-backtest
    itself: the run being gated is the one that writes the current month's
    accounting, so the accounting clock is held to the *previous* month
    regardless of the day (the freeze clock is unaffected — the panels this
    run consumes must already be frozen).

    The freeze clock is evaluated once PER ROOT and fails if ANY root is stale:
    the monthly freeze writes several evidence trees, and one healthy tree must
    never vouch for a stalled sibling.
    """
    root = Path(repo_root)
    ok = True
    lines: list[str] = []

    newest_acct = newest_accounting_month(root / forward_dir)
    acct_target = (
        previous_month(_month_of(today)) if pre_run else expected_month(today, ACCOUNTING_GRACE_DAY)
    )
    if newest_acct is None:
        lines.append(
            f"BOOTSTRAP accounting: no {forward_dir}/<YYYY-MM>/{ACCOUNTING_FILENAME} "
            "anywhere yet; the first monthly-forward-backtest run writes it"
        )
    elif newest_acct >= acct_target:
        lines.append(
            f"PASS accounting: newest accounted month {newest_acct} "
            f"satisfies expected {acct_target}"
        )
    else:
        ok = False
        lines.append(
            f"FAIL accounting: newest accounted month {newest_acct} is older than "
            f"expected {acct_target} — the monthly forward loop missed its cadence "
            "(disabled workflow, dead cron, or a run that never landed its commit)"
        )

    frz_target = expected_month(today, FREEZE_GRACE_DAY)
    for frozen_root in frozen_roots:
        newest_frz = newest_freeze_month(root / frozen_root)
        if newest_frz is None:
            lines.append(
                f"BOOTSTRAP freeze[{frozen_root}]: no dated panels under "
                f"{frozen_root}/<profile>/ yet"
            )
        elif newest_frz >= frz_target:
            lines.append(
                f"PASS freeze[{frozen_root}]: newest freeze month {newest_frz} "
                f"satisfies expected {frz_target}"
            )
        else:
            ok = False
            lines.append(
                f"FAIL freeze[{frozen_root}]: newest freeze month {newest_frz} is "
                f"older than expected {frz_target} — monthly-freeze missed its cadence "
                f"for this evidence root and every later forward window shrinks with it"
            )

    return ok, lines


def read_states_file(path: Path) -> dict[str, str]:
    """Parse the ``<profile>\\t<state>`` lines the workflow loop writes.

    Strict on purpose: unknown states, malformed lines, and duplicate
    profiles are refused rather than papered over — the states file is
    written by a five-branch shell loop, and a surprise here means the loop
    changed without this contract keeping up.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CadenceError(f"cannot read states file {path}: {exc}") from exc
    states: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) != 2 or not parts[0]:
            raise CadenceError(f"states file line {lineno} is not '<profile>\\t<state>': {raw!r}")
        profile, state = parts
        if state not in VALID_STATES:
            raise CadenceError(
                f"states file line {lineno}: unknown state {state!r} for {profile!r} "
                f"(valid: {', '.join(VALID_STATES)})"
            )
        if profile in states:
            raise CadenceError(f"states file line {lineno}: duplicate profile {profile!r}")
        states[profile] = state
    return states


def write_accounting(
    forward_dir: Path,
    month: str,
    states: dict[str, str],
    *,
    event: str = "manual",
    run_id: str | None = None,
    now: dt.datetime | None = None,
) -> Path:
    """Append this run's per-profile states to the month's accounting artifact.

    One file per month, one entry per run: a re-dispatch inside the same
    month appends a new entry to ``runs`` rather than erasing the earlier
    one, so no run's record is ever destroyed.  An existing file that fails
    to parse, names a different month, or carries an unknown schema_version
    is refused — never silently clobbered.
    """
    if not _MONTH_RE.match(month):
        raise CadenceError(f"month must be YYYY-MM, got {month!r}")
    if not states:
        raise CadenceError("no profile states to record — an empty loop is a bug, not a fact")
    for profile, state in states.items():
        if state not in VALID_STATES:
            raise CadenceError(
                f"unknown state {state!r} for {profile!r} (valid: {', '.join(VALID_STATES)})"
            )

    path = Path(forward_dir) / month / ACCOUNTING_FILENAME
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CadenceError(
                f"existing {path} does not parse ({exc}); refusing to overwrite it"
            ) from exc
        if payload.get("schema_version") != ACCOUNTING_SCHEMA_VERSION:
            raise CadenceError(
                f"existing {path} has schema_version "
                f"{payload.get('schema_version')!r}, not {ACCOUNTING_SCHEMA_VERSION!r}"
            )
        if payload.get("month") != month:
            raise CadenceError(
                f"existing {path} is for month {payload.get('month')!r}, not {month!r}"
            )
        runs = payload.get("runs")
        if not isinstance(runs, list):
            raise CadenceError(f"existing {path} has no 'runs' list")
    else:
        runs = []

    # timezone.utc, not the 3.11+ alias: the bare-script contract means this
    # module runs on whatever python a sibling repo's runner puts on PATH.
    stamp = now if now is not None else dt.datetime.now(dt.timezone.utc)  # noqa: UP017
    entry: dict[str, object] = {
        "run_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "profiles": dict(sorted(states.items())),
        "counts": {
            "evaluated": sum(1 for s in states.values() if s == "evaluated"),
            "not_matured": sum(1 for s in states.values() if s == "not_matured"),
            "refused": sum(1 for s in states.values() if s.startswith("refused")),
        },
    }
    if run_id:
        entry["run_id"] = str(run_id)

    payload = {
        "schema_version": ACCOUNTING_SCHEMA_VERSION,
        "month": month,
        "runs": [*runs, entry],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def run_check(
    *,
    repo_root: str = ".",
    pre_run: bool = False,
    as_of: str | None = None,
    frozen_roots: Sequence[str] | None = None,
    forward_dir: str = "docs/forward",
) -> int:
    if as_of is not None:
        try:
            today = dt.date.fromisoformat(as_of)
        except ValueError:
            print(f"--as-of must be an ISO date (YYYY-MM-DD), got {as_of!r}", file=sys.stderr)
            return 2
    else:
        # timezone.utc, not the 3.11+ alias — see write_accounting.
        today = dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017
    ok, lines = check_cadence(
        repo_root,
        today,
        pre_run=pre_run,
        frozen_roots=frozen_roots or FROZEN_ROOTS,
        forward_dir=forward_dir,
    )
    for line in lines:
        print(line)
    return 0 if ok else 1


def run_account(
    *,
    month: str,
    states_path: str,
    out: str = "docs/forward",
    event: str = "manual",
    run_id: str | None = None,
) -> int:
    try:
        states = read_states_file(Path(states_path))
        path = write_accounting(Path(out), month, states, event=event, run_id=run_id)
    except CadenceError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    counts = {
        "evaluated": sum(1 for s in states.values() if s == "evaluated"),
        "not_matured": sum(1 for s in states.values() if s == "not_matured"),
        "refused": sum(1 for s in states.values() if s.startswith("refused")),
    }
    print(
        f"accounted {len(states)} profile(s) for {month} -> {path} "
        f"(evaluated={counts['evaluated']} not_matured={counts['not_matured']} "
        f"refused={counts['refused']})"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cadence",
        description="expectation clocks for the monthly evidence loop",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser(
        "check", help="verify the accounting and freeze clocks (exit 1 on a miss)"
    )
    p_check.add_argument("--repo-root", default=".", help="Stock-Grader checkout root")
    p_check.add_argument(
        "--pre-run",
        action="store_true",
        help="self-gate mode: this run writes the current month's accounting, "
        "so hold the accounting clock to the previous month",
    )
    p_check.add_argument(
        "--as-of", default=None, help="ISO date to evaluate at (default: today UTC)"
    )
    p_check.add_argument(
        "--frozen-root",
        action="append",
        dest="frozen_roots",
        default=None,
        help="committed freeze evidence root to clock; repeatable "
        f"(default: {', '.join(FROZEN_ROOTS)})",
    )
    p_check.add_argument("--forward-dir", default="docs/forward")

    p_account = sub.add_parser(
        "account",
        help="append this run's per-profile states to the month's accounting artifact",
    )
    p_account.add_argument("--month", required=True, help="YYYY-MM being accounted")
    p_account.add_argument(
        "--states", required=True, help="TSV file of '<profile>\\t<state>' lines"
    )
    p_account.add_argument("--out", default="docs/forward")
    p_account.add_argument("--event", default="manual", help="what triggered the run")
    p_account.add_argument("--run-id", default=None, help="workflow run id, if any")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return run_check(
            repo_root=args.repo_root,
            pre_run=args.pre_run,
            as_of=args.as_of,
            frozen_roots=args.frozen_roots,
            forward_dir=args.forward_dir,
        )
    return run_account(
        month=args.month,
        states_path=args.states,
        out=args.out,
        event=args.event,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
