"""Locked, append-only research manifest for honest result artifacts.

Every edge/forecast experiment should leave a tamper-evident record so results
are reproducible and cannot be quietly p-hacked or re-spun. A record captures
exactly what the firewall cares about: the data span, symbols, targets,
horizons, declared trial count (for multiple-testing correction), cost
assumptions, the leakage controls used, the metrics, the benchmark it was
compared against, the code commit it ran at, and an explicit gate pass/fail with
an honest verdict.

The store is an **append-only JSONL log**: each line is one record plus a
SHA-256 integrity hash over its canonical payload. Every appended record also
carries ``prev_sha256`` -- the integrity hash of the line before it (a genesis
sentinel for the first line) -- and that link is *inside* the hashed payload, so
deleting, reordering or rewriting any line inside the chained suffix breaks the
chain visibly. A per-line hash alone only proves each surviving line is intact;
it cannot prove no line was quietly removed. Records are never mutated, so
the history of what was actually tested -- including the many honest negatives --
is preserved and verifiable. Pure stdlib; no result is ever marked a tradeable
edge unless the caller sets ``gate_passed`` after the full firewall is met.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "GENESIS_SHA256",
    "PREREGISTRATION_EXPERIMENT",
    "RETRACTION_EXPERIMENT",
    "ResearchRecord",
    "append_record",
    "current_commit",
    "find_preregistration",
    "load_manifest",
    "preregistered_experiment",
    "preregistration_record",
    "retracted_hashes",
    "spec_sha256",
    "summarize_manifest",
    "trial_sharpes",
    "trial_sharpes_by_experiment",
    "verify_chain",
    "verify_line",
]

# prev_sha256 of the very first chained record: there is no previous line to bind to.
GENESIS_SHA256 = "0" * 64

#: Experiment name marking a record that retracts earlier ones.
#:
#: The ledger is append-only and hash-chained, so a record that should never have
#: counted as a trial cannot be removed — it is retracted by a LATER record that
#: names it. The retracted hashes ride in ``symbols``, which is already a
#: ``Sequence[str]`` serialized verbatim by ``payload()``, so this needs no schema
#: change and the retraction is itself hashed and chained: what was excluded, when,
#: and why stays permanently auditable.
RETRACTION_EXPERIMENT = "ledger:retraction"

#: Experiment name marking a pre-registration declaration.
#:
#: A declaration records — BEFORE any evaluation runs — the exact spec of one
#: hypothesis (panel identity, config fingerprints, universe scope, horizon,
#: evaluation parameters) as a canonical-JSON blob in ``leakage_controls``,
#: its SHA-256 in ``symbols``, and the declared evaluation schedule in
#: ``verdict``. A later backtest whose observed spec hashes to the declared
#: value is a *primary re-evaluation* of that ONE pre-registered trial: it is
#: recorded under the declaration's stable experiment name, so the
#: collapse-to-latest rule in :func:`trial_sharpes` charges the hypothesis
#: exactly once no matter how many scheduled looks accrue. Declarations carry
#: no metrics, so they never enter the trial denominator themselves.
#:
#: Honesty note: pre-registration fixes trial-count inflation (selection
#: deflation over DISTINCT configurations). It does not correct sequential
#: looks at one accruing sample — that is optional stopping, a different
#: problem. The declared schedule makes the peeking *disclosed*, not
#: corrected; the ledger shows exactly when each look was declared to happen.
PREREGISTRATION_EXPERIMENT = "ledger:preregistration"


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
    # A metric may be None when it genuinely could not be computed (e.g. a panel
    # too short for a Sharpe): JSON null is loadable by every consumer, whereas
    # a serialized NaN poisons any later aggregate computed over the ledger.
    metrics: Mapping[str, float | None]
    costs: Mapping[str, float]
    benchmark: str
    leakage_controls: str
    gate_passed: bool
    verdict: str
    data_span: str = ""
    code_commit: str = "unknown"
    created_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Chain link, set by append_record at write time: the integrity_sha256 of the
    # previous ledger line (GENESIS_SHA256 for the first). Inside the payload --
    # and therefore inside this record's own hash -- so the link itself cannot be
    # rewritten to hide a deletion. None only for legacy pre-chain records.
    prev_sha256: str | None = None

    def payload(self) -> dict[str, object]:
        """Canonical, integrity-hashable view (excludes the hash itself)."""
        payload: dict[str, object] = {
            "experiment": self.experiment,
            "market": self.market,
            "symbols": list(self.symbols),
            "targets": list(self.targets),
            "horizons": list(self.horizons),
            "trials": int(self.trials),
            "metrics": {
                k: (None if v is None else float(v)) for k, v in self.metrics.items()
            },
            "costs": {k: float(v) for k, v in self.costs.items()},
            "benchmark": self.benchmark,
            "leakage_controls": self.leakage_controls,
            "gate_passed": bool(self.gate_passed),
            "verdict": self.verdict,
            "data_span": self.data_span,
            "code_commit": self.code_commit,
            "created_utc": self.created_utc,
        }
        # Additive: legacy records never carried the key, and their hashes must
        # keep verifying over the exact payload they were written with.
        if self.prev_sha256 is not None:
            payload["prev_sha256"] = self.prev_sha256
        return payload

    def integrity_sha256(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_line(self) -> str:
        return json.dumps(
            {**self.payload(), "integrity_sha256": self.integrity_sha256()}
        )


def _last_line_sha256(file_path: Path) -> str:
    """Integrity hash of the manifest's last record; genesis if none exists yet."""
    if not file_path.exists():
        return GENESIS_SHA256
    last = GENESIS_SHA256
    with file_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                last = str(json.loads(line).get("integrity_sha256", GENESIS_SHA256))
    return last


def append_record(path: str | Path, record: ResearchRecord) -> None:
    """Append a record to the append-only JSONL manifest (creating it if needed).

    The stored record is chained: its ``prev_sha256`` is set to the previous
    line's ``integrity_sha256`` (genesis for the first line), regardless of any
    value the caller put on the dataclass — the chain must reflect file order.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    chained = replace(record, prev_sha256=_last_line_sha256(file_path))
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(chained.to_line() + "\n")


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


def verify_chain(records: Sequence[dict[str, object]]) -> bool:
    """Check the prev_sha256 linkage over ``records`` in ledger order.

    A legacy prefix written before chaining existed is tolerated, but from the
    first record carrying ``prev_sha256`` on, every record must be chained, each
    link must point at the immediately preceding line's hash, and each chained
    line must still hash to its own claim — so deleting, reordering or rewriting
    any line inside the chained suffix is detected. Deleting the legacy prefix
    (or truncating the whole file at the final line) is outside what an
    intra-file chain can prove; anchor the head externally if that matters.
    """
    chain_started = False
    prev_hash = GENESIS_SHA256
    for record in records:
        claimed_prev = record.get("prev_sha256")
        if claimed_prev is None:
            if chain_started:
                return False  # an unchained line after chaining began is a splice
        else:
            chain_started = True
            if claimed_prev != prev_hash or not verify_line(record):
                return False
        prev_hash = str(record.get("integrity_sha256", ""))
    return True


def retracted_hashes(records: Sequence[Mapping[str, object]]) -> set[str]:
    """Integrity hashes named by every retraction record in the ledger."""
    retracted: set[str] = set()
    for record in records:
        if record.get("experiment") != RETRACTION_EXPERIMENT:
            continue
        symbols = record.get("symbols")
        if isinstance(symbols, Sequence) and not isinstance(symbols, (str, bytes)):
            retracted.update(str(item) for item in symbols)
    return retracted


def trial_sharpes_by_experiment(
    records: Sequence[Mapping[str, object]],
) -> dict[str, float]:
    """Most recent finite per-period Sharpe per DISTINCT experiment.

    The mapping form of :func:`trial_sharpes`, for callers that must REPLACE
    one experiment's entry with a fresher measurement (a pre-registered
    re-evaluation) instead of appending a near-duplicate: the shared
    denominator has to stay order-independent and flat across scheduled looks.
    """
    excluded = retracted_hashes(records)
    latest: dict[str, float] = {}
    for record in records:
        if record.get("experiment") == RETRACTION_EXPERIMENT:
            continue
        if str(record.get("integrity_sha256", "")) in excluded:
            continue
        metrics = record.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        sharpe = metrics.get("per_period_sharpe")
        if isinstance(sharpe, bool) or not isinstance(sharpe, (int, float)):
            continue
        if not math.isfinite(float(sharpe)):
            continue
        latest[str(record.get("experiment", ""))] = float(sharpe)
    return latest


def trial_sharpes(records: Sequence[Mapping[str, object]]) -> list[float]:
    """Per-period Sharpes of the DISTINCT configurations searched.

    Retracted records are excluded, as are the retractions themselves. What
    remains is collapsed to the most recent entry per ``experiment``: re-running
    one profile every month on a longer sample is that trial measured again, not
    a new trial, and counting it twelve times a year inflates the deflation
    benchmark until no real edge could ever clear it.

    Legacy records with distinct experiment names collapse to themselves, so this
    is backward compatible.
    """
    return list(trial_sharpes_by_experiment(records).values())


# -- pre-registration ----------------------------------------------------------


def spec_sha256(spec: Mapping[str, object]) -> str:
    """SHA-256 over the canonical JSON serialization of a hypothesis spec."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def preregistered_experiment(spec: Mapping[str, object]) -> str:
    """Stable experiment name for every primary re-evaluation of one spec.

    Embeds the spec hash, never a file name: the identity of a pre-registered
    hypothesis must not depend on what the panel file happened to be called.
    """
    profiles = spec.get("profiles")
    label = (
        ",".join(str(p) for p in profiles)
        if isinstance(profiles, Sequence) and not isinstance(profiles, (str, bytes)) and profiles
        else "panel"
    )
    return f"backtest:preregistered:{label}:{spec_sha256(spec)[:12]}"


def preregistration_record(
    spec: Mapping[str, object],
    *,
    schedule: str,
    market: str = "us_equities",
    code_commit: str | None = None,
) -> ResearchRecord:
    """Build a declaration record for ``spec`` (the caller appends it).

    Rides entirely on the existing schema, like retraction records: the spec's
    hash in ``symbols``, the canonical spec JSON in ``leakage_controls`` (so
    the hash is recomputable from the record itself), the declared evaluation
    schedule in ``verdict``. No metrics — a declaration is not a measurement
    and must never enter the trial denominator.
    """
    horizons = spec.get("horizon_days")
    return ResearchRecord(
        experiment=PREREGISTRATION_EXPERIMENT,
        market=market,
        symbols=[spec_sha256(spec)],
        targets=["forward_return"],
        horizons=(
            [int(h) for h in horizons]
            if isinstance(horizons, Sequence) and not isinstance(horizons, (str, bytes))
            else []
        ),
        trials=0,
        metrics={},
        costs={},
        benchmark="zero",
        leakage_controls=json.dumps(spec, sort_keys=True, separators=(",", ":")),
        gate_passed=False,
        verdict=(
            f"pre-registered {preregistered_experiment(spec)}; evaluation schedule: "
            f"{schedule}; sequential looks at this one accruing sample are declared "
            f"— disclosed peeking, not corrected"
        ),
        code_commit=code_commit if code_commit is not None else current_commit(),
    )


def find_preregistration(
    records: Sequence[Mapping[str, object]], spec: Mapping[str, object]
) -> dict[str, object] | None:
    """Newest unretracted declaration whose spec hash matches ``spec``.

    Refuses to match a tampered declaration: the record line must still hash
    to its own claim AND the spec JSON stored in ``leakage_controls`` must
    recompute to the hash claimed in ``symbols``. A declaration whose stored
    spec and claimed hash disagree is treated as absent — a lying declaration
    must not bless anything as pre-registered.
    """
    target = spec_sha256(spec)
    excluded = retracted_hashes(records)
    match: dict[str, object] | None = None
    for record in records:
        if record.get("experiment") != PREREGISTRATION_EXPERIMENT:
            continue
        if str(record.get("integrity_sha256", "")) in excluded:
            continue
        symbols = record.get("symbols")
        if not (
            isinstance(symbols, Sequence)
            and not isinstance(symbols, (str, bytes))
            and target in {str(item) for item in symbols}
        ):
            continue
        if not verify_line(dict(record)):
            continue
        try:
            stored_spec = json.loads(str(record.get("leakage_controls", "")))
        except ValueError:
            continue
        if not isinstance(stored_spec, Mapping) or spec_sha256(stored_spec) != target:
            continue
        match = dict(record)
    return match


def summarize_manifest(records: Sequence[dict[str, object]]) -> dict[str, object]:
    """Aggregate pass/fail counts and integrity status over loaded records."""
    passed = sum(1 for r in records if r.get("gate_passed"))
    chain_ok = verify_chain(records)
    intact = all(verify_line(r) for r in records) and chain_ok
    return {
        "total": len(records),
        "gate_passed": passed,
        "gate_failed": len(records) - passed,
        "all_integrity_ok": intact,
        "chain_ok": chain_ok,
        "experiments": [r.get("experiment", "?") for r in records],
    }
