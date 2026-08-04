"""Append-only run journal: run-over-run memory for grades.

``stock-grader grade`` appends each run's reports as one JSON record under
``~/.stock-grader/runs/`` (``--journal-dir`` overrides, ``--no-journal``
disables). The journal is what makes two already-shipped behaviours reachable
from the CLI:

* Letter hysteresis — ``scoring.apply_hysteresis`` needs the previous run's
  letters, and until this journal existed no CLI path could supply them, so a
  score drifting between 70.9 and 71.1 flipped B+/B on every refresh.
* ``stock-grader diff --since-last TICKER`` — needs a baseline run to report
  letter/score/pillar deltas and the metric contributions that moved them.

Immutability: records are appended as new files and never rewritten. A name
collision bumps the record's timestamp by a microsecond rather than
overwriting — the journal is evidence of what past runs said, so an existing
record is never touched. Frozen panels deliberately do NOT read the journal:
a frozen score must be a function of its inputs, not of whatever mutable
local state accumulated on one machine.

Comparability: two scores are comparable only within one fingerprint regime
(ECOSYSTEM rule 3). The config fingerprint must match exactly. The canonical
universe fingerprint additionally hashes each member's ``asof`` — the data
vintage — so two honest runs on different days legitimately differ there. The
journal therefore also records a vintage-free *membership* fingerprint over
``(ticker, cik, sector)``: hysteresis and ``diff`` require config and
membership to match, while vintage movement is exactly the change being
reported.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .report import to_json
from .types import GradeReport, SecuritySnapshot

__all__ = [
    "DEFAULT_JOURNAL_DIR",
    "JournalError",
    "append_run",
    "comparability_mismatches",
    "diff_reports",
    "iter_runs_newest_first",
    "load_run",
    "membership_fingerprint",
    "previous_letters",
    "report_members",
    "resolve_since_last",
    "snapshot_members",
]

DEFAULT_JOURNAL_DIR = Path("~/.stock-grader/runs")
SCHEMA_VERSION = "1.0"


class JournalError(RuntimeError):
    """A journal operation that cannot be performed honestly.

    Raised instead of degrading silently: an unjournalable run, an empty or
    corrupt journal, or a baseline that is not comparable.
    """


def snapshot_members(snapshots: Iterable[SecuritySnapshot]) -> list[list[str]]:
    """Vintage-free membership rows for the universe about to be graded."""
    return sorted(
        [snapshot.ticker.upper(), snapshot.cik or "", snapshot.sector.value]
        for snapshot in snapshots
    )


def report_members(reports: Mapping[str, GradeReport]) -> list[list[str]]:
    """Vintage-free membership rows recovered from a graded run's reports.

    Must agree with :func:`snapshot_members` for the same universe: the
    pipeline copies ``snapshot.cik`` and ``snapshot.sector.value`` into each
    report's meta verbatim.
    """
    return sorted(
        [
            report.ticker.upper(),
            str(report.meta.get("cik") or ""),
            str(report.meta.get("sector") or ""),
        ]
        for report in reports.values()
    )


def membership_fingerprint(members: Iterable[Iterable[str]]) -> str:
    """Hash of sorted (ticker, cik, sector) rows — the peer-set identity.

    Unlike the canonical universe fingerprint this excludes each member's
    ``asof``, so it is stable across data vintages of the same peer set.
    """
    rows = sorted([str(field) for field in row] for row in members)
    encoded = json.dumps(rows, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _single_meta_value(reports: Mapping[str, GradeReport], key: str) -> str:
    values = {str(report.meta.get(key)) for report in reports.values() if report.meta.get(key)}
    if not values:
        raise JournalError(
            f"run carries no {key} (every report's meta lacks it) — an "
            f"unfingerprinted run cannot serve as a comparison baseline, so it "
            f"is not journaled"
        )
    if len(values) > 1:
        raise JournalError(f"run mixes {key} values ({sorted(values)}); refusing to journal")
    return values.pop()


def append_run(
    reports: Mapping[str, GradeReport],
    *,
    journal_dir: str | Path,
    command: str = "grade",
    recorded_at: datetime | None = None,
) -> Path:
    """Append one run's reports as a new immutable journal record.

    Returns the path written. Raises :class:`JournalError` for a run that
    cannot honestly serve as a baseline (no reports, missing or mixed
    fingerprints) — the caller reports that and moves on; the grade output
    itself is unaffected.
    """
    if not reports:
        raise JournalError("empty run: nothing to journal")
    config_fp = _single_meta_value(reports, "config_fingerprint")
    universe_fp = _single_meta_value(reports, "universe_fingerprint")
    profiles = {report.profile for report in reports.values()}
    if len(profiles) > 1:
        raise JournalError(f"run mixes profiles ({sorted(profiles)}); refusing to journal")
    members = report_members(reports)
    asof = max(report.asof for report in reports.values())
    when = recorded_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    record = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "asof": asof.isoformat(),
        "profile": profiles.pop(),
        "config_fingerprint": config_fp,
        "universe_fingerprint": universe_fp,
        "membership_fingerprint": membership_fingerprint(members),
        "members": members,
        "reports": json.loads(to_json(dict(reports))),
    }

    directory = Path(journal_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    # Timestamped names sort lexically in chronological order. On a collision
    # the timestamp advances one microsecond: append-only means an existing
    # record is never overwritten, under any race.
    while True:
        stamp = when.strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{stamp}_{asof.isoformat()}_{config_fp[:12]}_{universe_fp[:12]}.json"
        if not path.exists():
            break
        when += timedelta(microseconds=1)
    record["recorded_at_utc"] = when.isoformat()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_run(path: Path) -> dict[str, Any]:
    """Parse one journal record, refusing loudly on corruption.

    A record that does not parse is evidence of tampering or a torn write
    (writes are atomic, so the latter should not happen); it is named rather
    than skipped so the operator removes it deliberately.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JournalError(f"unreadable journal record {path}: {exc}") from exc
    if not isinstance(record, dict) or "reports" not in record:
        raise JournalError(f"journal record {path} lacks a reports section")
    return record


def iter_runs_newest_first(journal_dir: str | Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield (path, record) newest first; parsing is lazy, one file at a time."""
    directory = Path(journal_dir).expanduser()
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json"), reverse=True):
        yield path, load_run(path)


def previous_letters(
    journal_dir: str | Path,
    *,
    config_fingerprint: str,
    membership_fingerprint: str,
) -> dict[str, str]:
    """Letters from the newest journaled run in the same comparability regime.

    Empty when no run matches: hysteresis then simply does not engage, which
    is the correct behaviour for a first run or after a config/universe
    change — a letter from a different regime must not stabilise this one.
    """
    for _path, record in iter_runs_newest_first(journal_dir):
        if record.get("config_fingerprint") != config_fingerprint:
            continue
        if record.get("membership_fingerprint") != membership_fingerprint:
            continue
        return {
            ticker: report["letter"]
            for ticker, report in record["reports"].items()
            if report.get("letter") and report.get("letter") != "N/A"
        }
    return {}


def resolve_since_last(
    journal_dir: str | Path, ticker: str
) -> tuple[tuple[Path, dict[str, Any]], tuple[Path, dict[str, Any]]]:
    """Resolve ``--since-last``: (baseline, current) = the two newest runs holding ``ticker``.

    The current run is the newest journaled run containing the ticker; the
    baseline is the next older one. Comparability between them is the caller's
    check (:func:`comparability_mismatches`) so the refusal can say what
    drifted.
    """
    ticker = ticker.upper()
    found: list[tuple[Path, dict[str, Any]]] = []
    directory = Path(journal_dir).expanduser()
    if not directory.is_dir():
        raise JournalError(
            f"no run journal at {directory} — run `stock-grader grade {ticker}` "
            f"(without --no-journal) to start one"
        )
    for path, record in iter_runs_newest_first(directory):
        if ticker in record["reports"]:
            found.append((path, record))
            if len(found) == 2:
                break
    if not found:
        raise JournalError(f"no journaled run contains {ticker} under {directory}")
    if len(found) == 1:
        raise JournalError(
            f"only one journaled run contains {ticker} (recorded "
            f"{found[0][1].get('recorded_at_utc', 'unknown')}); a diff needs a "
            f"second run to compare against"
        )
    current, baseline = found
    return baseline, current


def comparability_mismatches(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Regime breaks between two runs; empty when a diff is honest.

    Config fingerprints must match exactly, and so must the vintage-free
    membership fingerprint. The canonical universe fingerprint is allowed to
    differ — it hashes each member's asof, and data-vintage movement is
    exactly what a diff reports.
    """
    mismatches: list[str] = []
    base_config = str(baseline.get("config_fingerprint", ""))
    cur_config = str(current.get("config_fingerprint", ""))
    if base_config != cur_config:
        mismatches.append(
            f"config fingerprint changed ({base_config[:12]}… → {cur_config[:12]}…; "
            f"profile {baseline.get('profile')} → {current.get('profile')})"
        )
    if baseline.get("membership_fingerprint") != current.get("membership_fingerprint"):
        base_members = {tuple(row) for row in baseline.get("members", [])}
        cur_members = {tuple(row) for row in current.get("members", [])}
        added = sorted(row[0] for row in cur_members - base_members)
        removed = sorted(row[0] for row in base_members - cur_members)
        detail = []
        if added:
            detail.append(f"added {', '.join(added[:6])}{'…' if len(added) > 6 else ''}")
        if removed:
            detail.append(f"removed {', '.join(removed[:6])}{'…' if len(removed) > 6 else ''}")
        mismatches.append(
            "universe membership changed" + (f" ({'; '.join(detail)})" if detail else "")
        )
    return mismatches


def _finite(value: Any) -> float | None:
    """JSON-decoded numbers only: the encoder already mapped NaN/inf to null."""
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _delta(old: float | None, new: float | None) -> float | None:
    if old is None or new is None:
        return None
    return new - old


def _scalar_diff(old: Any, new: Any) -> dict[str, Any]:
    old_value, new_value = _finite(old), _finite(new)
    return {"from": old_value, "to": new_value, "delta": _delta(old_value, new_value)}


def diff_reports(baseline: dict[str, Any], current: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Letter/score/pillar deltas plus the metric contributions that moved them.

    Operates on two journal records (already comparability-checked). Metric
    movers are sorted by absolute contribution change, so the first rows
    answer "what moved this grade" directly.
    """
    ticker = ticker.upper()
    old = baseline["reports"][ticker]
    new = current["reports"][ticker]

    pillar_names = sorted(set(old.get("pillars", {})) | set(new.get("pillars", {})))
    pillars = {
        name: _scalar_diff(
            old.get("pillars", {}).get(name, {}).get("score"),
            new.get("pillars", {}).get(name, {}).get("score"),
        )
        for name in pillar_names
    }

    old_contrib = old.get("explain", {}).get("metric_contributions", {}) or {}
    new_contrib = new.get("explain", {}).get("metric_contributions", {}) or {}
    movers = []
    for metric in sorted(set(old_contrib) | set(new_contrib)):
        entry = _scalar_diff(old_contrib.get(metric), new_contrib.get(metric))
        # A metric present on one side only still moved the grade: its whole
        # contribution appeared or vanished.
        if entry["delta"] is None:
            entry["delta"] = (entry["to"] or 0.0) - (entry["from"] or 0.0)
        entry["metric"] = metric
        movers.append(entry)
    movers.sort(key=lambda entry: (-abs(entry["delta"]), entry["metric"]))

    def _run_header(path_record: dict[str, Any]) -> dict[str, Any]:
        return {
            "recorded_at_utc": path_record.get("recorded_at_utc"),
            "asof": path_record.get("asof"),
            "config_fingerprint": path_record.get("config_fingerprint"),
            "universe_fingerprint": path_record.get("universe_fingerprint"),
        }

    return {
        "ticker": ticker,
        "profile": current.get("profile"),
        "baseline": _run_header(baseline),
        "current": _run_header(current),
        "letter": {
            "from": old.get("letter"),
            "to": new.get("letter"),
            "changed": old.get("letter") != new.get("letter"),
        },
        "score": _scalar_diff(old.get("score"), new.get("score")),
        "percentile": _scalar_diff(old.get("percentile"), new.get("percentile")),
        "coverage": _scalar_diff(old.get("coverage"), new.get("coverage")),
        "pillars": pillars,
        "metric_movers": movers,
    }
