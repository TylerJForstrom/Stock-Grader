"""Locked, append-only research manifest for honest result artifacts.

Every edge/forecast experiment should leave a tamper-evident record so results
are reproducible and cannot be quietly p-hacked or re-spun. A record captures
exactly what the firewall cares about: the data span, symbols, targets,
horizons, declared trial count (for multiple-testing correction), cost
assumptions, the leakage controls used, the metrics, the benchmark it was
compared against, the code commit it ran at, and an explicit gate pass/fail with
an honest verdict.

The store is an **append-only JSONL log**: each line is one record plus a
SHA-256 integrity hash over its canonical payload. Records are never mutated, so
the history of what was actually tested -- including the many honest negatives --
is preserved and verifiable. Pure stdlib; no result is ever marked a tradeable
edge unless the caller sets ``gate_passed`` after the full firewall is met.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "ResearchRecord",
    "append_record",
    "current_commit",
    "load_manifest",
    "summarize_manifest",
    "verify_line",
]


def current_commit() -> str:
    """Best-effort short git commit hash for provenance; 'unknown' if unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class ResearchRecord:
    experiment: str
    market: str
    symbols: Sequence[str]
    targets: Sequence[str]
    horizons: Sequence[int]
    trials: int  # declared research grid size, for multiple-testing correction
    metrics: Mapping[str, float]
    costs: Mapping[str, float]
    benchmark: str
    leakage_controls: str
    gate_passed: bool
    verdict: str
    data_span: str = ""
    code_commit: str = "unknown"
    created_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def payload(self) -> dict[str, object]:
        """Canonical, integrity-hashable view (excludes the hash itself)."""
        return {
            "experiment": self.experiment,
            "market": self.market,
            "symbols": list(self.symbols),
            "targets": list(self.targets),
            "horizons": list(self.horizons),
            "trials": int(self.trials),
            "metrics": {k: float(v) for k, v in self.metrics.items()},
            "costs": {k: float(v) for k, v in self.costs.items()},
            "benchmark": self.benchmark,
            "leakage_controls": self.leakage_controls,
            "gate_passed": bool(self.gate_passed),
            "verdict": self.verdict,
            "data_span": self.data_span,
            "code_commit": self.code_commit,
            "created_utc": self.created_utc,
        }

    def integrity_sha256(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_line(self) -> str:
        return json.dumps(
            {**self.payload(), "integrity_sha256": self.integrity_sha256()}
        )


def append_record(path: str | Path, record: ResearchRecord) -> None:
    """Append a record to the append-only JSONL manifest (creating it if needed)."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_line() + "\n")


def load_manifest(path: str | Path) -> list[dict[str, object]]:
    """Load all records (as dicts) from the manifest, oldest first."""
    file_path = Path(path)
    if not file_path.exists():
        return []
    records: list[dict[str, object]] = []
    with file_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def verify_line(record: dict[str, object]) -> bool:
    """Recompute the integrity hash and check it matches (tamper detection)."""
    claimed = record.get("integrity_sha256")
    if not claimed:
        return False
    payload = {k: v for k, v in record.items() if k != "integrity_sha256"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() == claimed


def summarize_manifest(records: Sequence[dict[str, object]]) -> dict[str, object]:
    """Aggregate pass/fail counts and integrity status over loaded records."""
    passed = sum(1 for r in records if r.get("gate_passed"))
    intact = all(verify_line(r) for r in records)
    return {
        "total": len(records),
        "gate_passed": passed,
        "gate_failed": len(records) - passed,
        "all_integrity_ok": intact,
        "experiments": [r.get("experiment", "?") for r in records],
    }
