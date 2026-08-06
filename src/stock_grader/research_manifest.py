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
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "EVIDENCE_SHA256_RE",
    "GENESIS_SHA256",
    "PREREGISTRATION_EXPERIMENT",
    "PROMOTION_EXPERIMENT",
    "PROMOTION_STAGES",
    "RETIRED_STAGE",
    "RETRACTION_EXPERIMENT",
    "ResearchRecord",
    "append_record",
    "current_commit",
    "declared_policies",
    "find_preregistration",
    "find_promotion_policy",
    "load_manifest",
    "package_commit",
    "preregistered_experiment",
    "preregistration_record",
    "promotion_policy_declaration",
    "promotion_policy_record",
    "promotion_stage",
    "promotion_transition_record",
    "retracted_hashes",
    "spec_sha256",
    "summarize_manifest",
    "trial_sharpes",
    "trial_sharpes_by_experiment",
    "validate_promotion_transition",
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

#: Experiment name marking a promotion-lifecycle record.
#:
#: Two record kinds ride this experiment name, both additive on the existing
#: schema exactly like retraction and preregistration (a self-hash in
#: ``symbols``, the canonical declaration JSON in ``leakage_controls``, no
#: metrics so neither kind can ever enter the trial denominator):
#:
#: - a **policy declaration** binds a versioned promotion-policy document
#:   (``docs/PROMOTION.md``) by sha256. Amendments are a NEW policy version
#:   declared by a NEW record — superseded declarations stay in the chain.
#: - a **stage transition** records one subject (a spec hash) moving between
#:   declared lifecycle stages under a named policy version, citing the
#:   integrity hashes of the evidence records the decision rests on.
#:
#: Licensing split (the load-bearing design point): this PUBLIC ledger carries
#: only spec hashes, stage names, the policy version, and evidence-record
#: integrity hashes. A sha256 is not a derived result. The NUMERIC evidence
#: behind decisions about license-walled vault signals (Finnhub/FINRA/IB/
#: SSGA/Massive-derived) lives in Stock-Vault's append-only decision journal
#: (``stock_vault.decisions``), which these records reference by record hash
#: and chain head — never by value.
PROMOTION_EXPERIMENT = "ledger:promotion"

#: The promotion ladder, lowest rung first. ``retired`` is a terminal state
#: outside the ladder: a retired subject never re-enters — revival means a new
#: spec, a new subject hash, and a new trial charged.
PROMOTION_STAGES = (
    "exploratory",
    "declared_trial",
    "shadow_arm",
    "paper_default",
    "live_money",
)

RETIRED_STAGE = "retired"

#: An evidence pointer is a ledger line's ``integrity_sha256`` — 64 lowercase
#: hex characters, nothing else. A length-only check let ``[""]`` satisfy "every
#: upward transition must name the records it rests on".
EVIDENCE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def current_commit(repo_dir: str | Path | None = None) -> str:
    """Best-effort short git commit hash for provenance; 'unknown' if unavailable.

    With no ``repo_dir`` this reads the PROCESS working directory, which is the
    right answer for every in-repo caller (ledger records, built panels). A
    cross-repo caller — the signal-panel join runs with a Stock-Vault checkout
    as CWD — must pass the tree it means, or use :func:`package_commit`.
    """
    command = ["git", "rev-parse", "--short", "HEAD"]
    if repo_dir is not None:
        command = ["git", "-C", str(repo_dir), *command[1:]]
    try:
        out = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def package_commit() -> str:
    """Revision of THIS package's source tree, independent of the CWD.

    ``builder_commit`` on an artifact must identify the code that produced it.
    When the grader runs from a sibling repo's checkout (the v6 signal-panel
    join runs with the vault as CWD) a bare ``git rev-parse`` names the WRONG
    repository, and the resulting stamp resolves in neither tree's history.

    Resolution order, each honest about what it is:

    1. ``STOCK_GRADER_COMMIT`` — the pin a workflow records when it checks this
       package out at an explicit ref (a non-editable ``pip install`` leaves no
       git tree to interrogate, so nothing else can recover it);
    2. the git revision of the source tree this module lives in;
    3. ``dist:<version>`` from the installed distribution — deliberately
       prefixed so it can never be mistaken for a commit hash;
    4. ``"unknown"``. Never a hash belonging to some other repository.
    """
    pinned = os.environ.get("STOCK_GRADER_COMMIT", "").strip()
    if pinned:
        return pinned
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        commit = current_commit(repo_dir=root)
        if commit != "unknown":
            return commit
    try:
        from importlib.metadata import version

        return f"dist:{version('stock-grader')}"
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
            "metrics": {k: (None if v is None else float(v)) for k, v in self.metrics.items()},
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
        return json.dumps({**self.payload(), "integrity_sha256": self.integrity_sha256()})


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


def append_record(path: str | Path, record: ResearchRecord) -> ResearchRecord:
    """Append a record to the append-only JSONL manifest (creating it if needed).

    The stored record is chained: its ``prev_sha256`` is set to the previous
    line's ``integrity_sha256`` (genesis for the first line), regardless of any
    value the caller put on the dataclass — the chain must reflect file order.

    Returns the CHAINED record — the one actually written. ``prev_sha256`` is
    inside ``payload()``, so the argument's ``integrity_sha256()`` is never the
    hash on disk: a caller that reports or stores the pre-append hash publishes
    an evidence pointer that resolves to nothing. Report this return value.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    chained = replace(record, prev_sha256=_last_line_sha256(file_path))
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(chained.to_line() + "\n")
    return chained


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

    A line that does not hash to its own claim is skipped, exactly as
    :func:`find_preregistration` and :func:`_promotion_declarations` skip one:
    an edited ``per_period_sharpe`` must not be allowed to set the deflation
    dispersion that decides whether a real edge clears its gate.
    """
    excluded = retracted_hashes(records)
    latest: dict[str, float] = {}
    for record in records:
        if record.get("experiment") == RETRACTION_EXPERIMENT:
            continue
        if str(record.get("integrity_sha256", "")) in excluded:
            continue
        if not verify_line(dict(record)):
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


# -- promotion lifecycle -------------------------------------------------------


def promotion_policy_declaration(
    *,
    policy_version: str,
    policy_doc: str,
    policy_sha256: str,
    stages: Sequence[str] = PROMOTION_STAGES,
    live_money_reachable: bool = False,
) -> dict[str, object]:
    """Canonical machine-readable core of one promotion-policy version.

    Only what the ledger must ENFORCE is machine-encoded (the stage ladder and
    whether the live-money rung is reachable); every numeric threshold lives in
    the policy document itself, whose bytes are bound by ``policy_sha256`` —
    editing the document without declaring a new version breaks every later
    transition against it.
    """
    return {
        "kind": "promotion-policy",
        "policy_version": policy_version,
        "policy_doc": policy_doc,
        "policy_sha256": policy_sha256,
        "stages": list(stages),
        "live_money_reachable": bool(live_money_reachable),
    }


def promotion_policy_record(
    declaration: Mapping[str, object],
    *,
    market: str = "us_equities",
    code_commit: str | None = None,
) -> ResearchRecord:
    """Build a policy-declaration record (the caller appends it).

    ``symbols[0]`` is the sha256 of the canonical declaration JSON stored in
    ``leakage_controls`` (recomputable from the record itself, exactly the
    preregistration tamper contract); ``symbols[1]`` is the policy document's
    own sha256, so the doc is greppable by hash. No metrics — a policy is not
    a measurement and must never enter the trial denominator.
    """
    return ResearchRecord(
        experiment=PROMOTION_EXPERIMENT,
        market=market,
        symbols=[spec_sha256(declaration), str(declaration.get("policy_sha256", ""))],
        targets=[],
        horizons=[],
        trials=0,
        metrics={},
        costs={},
        benchmark="none",
        leakage_controls=json.dumps(declaration, sort_keys=True, separators=(",", ":")),
        gate_passed=False,
        verdict=(
            f"PROMOTION-POLICY {declaration.get('policy_version')} declared; "
            f"doc {declaration.get('policy_doc')} "
            f"sha256 {declaration.get('policy_sha256')}; amendment only by a NEW "
            f"version — superseded declarations stay in the chain"
        ),
        code_commit=code_commit if code_commit is not None else current_commit(),
    )


def promotion_transition_record(
    transition: Mapping[str, object],
    *,
    market: str = "us_equities",
    code_commit: str | None = None,
) -> ResearchRecord:
    """Build a stage-transition record (validate first, then append).

    ``symbols`` carries hashes only — the transition's own self-hash, the
    subject's spec hash, then every cited evidence-record integrity hash — so
    the public line holds no derived results. The full declaration JSON rides
    in ``leakage_controls`` for recomputation.
    """
    evidence = transition.get("evidence_sha256")
    evidence_list = (
        [str(item) for item in evidence]
        if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes))
        else []
    )
    from_stage = str(transition.get("from_stage", ""))
    to_stage = str(transition.get("to_stage", ""))
    if to_stage == RETIRED_STAGE:
        action = "RETIREMENT"
    elif (
        to_stage in PROMOTION_STAGES
        and from_stage in PROMOTION_STAGES
        and PROMOTION_STAGES.index(to_stage) > PROMOTION_STAGES.index(from_stage)
    ):
        action = "PROMOTION"
    else:
        action = "DEMOTION"
    return ResearchRecord(
        experiment=PROMOTION_EXPERIMENT,
        market=market,
        symbols=[
            spec_sha256(transition),
            str(transition.get("subject_spec_sha256", "")),
            *evidence_list,
        ],
        targets=[],
        horizons=[],
        trials=0,
        metrics={},
        costs={},
        benchmark="none",
        leakage_controls=json.dumps(transition, sort_keys=True, separators=(",", ":")),
        gate_passed=False,
        verdict=(
            f"{action}: {transition.get('subject')} "
            f"{from_stage} -> {to_stage} under "
            f"{transition.get('policy_version')}; {transition.get('reason')}"
        ),
        code_commit=code_commit if code_commit is not None else current_commit(),
    )


def _promotion_declarations(
    records: Sequence[Mapping[str, object]],
) -> list[tuple[dict[str, object], dict[str, object]]]:
    """(record, parsed declaration) for every trustworthy promotion record.

    Trustworthy means: unretracted, the line still hashes to its own claim,
    and the declaration JSON in ``leakage_controls`` recomputes to the
    self-hash claimed in ``symbols[0]`` — a lying record must neither bless a
    policy nor move a stage, mirroring :func:`find_preregistration`.
    """
    excluded = retracted_hashes(records)
    out: list[tuple[dict[str, object], dict[str, object]]] = []
    for record in records:
        if record.get("experiment") != PROMOTION_EXPERIMENT:
            continue
        if str(record.get("integrity_sha256", "")) in excluded:
            continue
        if not verify_line(dict(record)):
            continue
        symbols = record.get("symbols")
        if not (isinstance(symbols, Sequence) and not isinstance(symbols, (str, bytes))):
            continue
        claimed = [str(item) for item in symbols]
        try:
            declaration = json.loads(str(record.get("leakage_controls", "")))
        except ValueError:
            continue
        if not isinstance(declaration, Mapping):
            continue
        if not claimed or spec_sha256(declaration) != claimed[0]:
            continue
        out.append((dict(record), dict(declaration)))
    return out


def declared_policies(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Every trustworthy promotion-policy declaration, oldest first.

    The version<->document binding is checkable in both directions only when a
    caller can see what ELSE a document's bytes are already declared as.
    """
    return [
        declaration
        for _, declaration in _promotion_declarations(records)
        if declaration.get("kind") == "promotion-policy"
    ]


def find_promotion_policy(
    records: Sequence[Mapping[str, object]], policy_version: str
) -> dict[str, object] | None:
    """Newest trustworthy declaration of ``policy_version`` (parsed), or None."""
    match: dict[str, object] | None = None
    for _, declaration in _promotion_declarations(records):
        if declaration.get("kind") != "promotion-policy":
            continue
        if str(declaration.get("policy_version", "")) == policy_version:
            match = declaration
    return match


def promotion_stage(
    records: Sequence[Mapping[str, object]],
    subject_spec_sha256: str,
    *,
    stages: Sequence[str] | None = None,
) -> str:
    """The subject's current lifecycle stage: its newest trustworthy
    transition's ``to_stage``, else the ladder's bottom rung.

    ``stages`` is the DECLARED ladder of the policy the caller is validating
    against. Seeding from the module constant instead would make a policy whose
    bottom rung is not ``PROMOTION_STAGES[0]`` unenterable: every untouched
    subject would report a stage absent from its own ladder, and both the
    honest ``--from-stage`` and the constant's value would be refused.
    ``PROMOTION_STAGES[0]`` remains the fallback when no policy is in hand.
    """
    ladder = [str(s) for s in stages] if stages else list(PROMOTION_STAGES)
    stage = ladder[0] if ladder else PROMOTION_STAGES[0]
    for _, declaration in _promotion_declarations(records):
        if declaration.get("kind") != "stage-transition":
            continue
        if str(declaration.get("subject_spec_sha256", "")) != subject_spec_sha256:
            continue
        stage = str(declaration.get("to_stage", stage))
    return stage


def validate_promotion_transition(
    records: Sequence[Mapping[str, object]], transition: Mapping[str, object]
) -> str | None:
    """Why ``transition`` may NOT be appended, or None if it may.

    Enforced against the ledger itself, not against code constants: the rules
    come from the declared policy record the transition names, so amending
    behavior requires declaring a new policy version into the chain.
    """
    subject = str(transition.get("subject_spec_sha256", ""))
    from_stage = str(transition.get("from_stage", ""))
    to_stage = str(transition.get("to_stage", ""))
    reason = str(transition.get("reason", "")).strip()
    policy_version = str(transition.get("policy_version", ""))
    if not subject:
        return "transition names no subject_spec_sha256"
    if not reason:
        return "transition carries no reason"
    policy = find_promotion_policy(records, policy_version)
    if policy is None:
        return (
            f"policy {policy_version!r} is not declared in this ledger; "
            f"declare the policy before any transition under it"
        )
    if str(transition.get("policy_sha256", "")) != str(policy.get("policy_sha256", "")):
        return (
            f"policy document hash mismatch: the transition was built against "
            f"{str(transition.get('policy_sha256', ''))[:12]} but "
            f"{policy_version} declared {str(policy.get('policy_sha256', ''))[:12]}"
        )
    stages_obj = policy.get("stages")
    stages = (
        [str(s) for s in stages_obj]
        if isinstance(stages_obj, Sequence) and not isinstance(stages_obj, (str, bytes))
        else list(PROMOTION_STAGES)
    )
    current = promotion_stage(records, subject, stages=stages)
    if current == RETIRED_STAGE:
        return (
            f"subject {subject[:12]} is retired — terminal; revival requires a "
            f"new spec (a new subject hash) and a new trial"
        )
    if from_stage != current:
        return f"from_stage {from_stage!r} does not match the subject's recorded stage {current!r}"
    if to_stage == RETIRED_STAGE:
        return None
    if to_stage not in stages:
        return f"unknown to_stage {to_stage!r}; policy stages: {', '.join(stages)}"
    if from_stage not in stages:
        return f"unknown from_stage {from_stage!r}; policy stages: {', '.join(stages)}"
    delta = stages.index(to_stage) - stages.index(from_stage)
    if delta == 0:
        return f"{from_stage!r} -> {to_stage!r} is not a transition"
    if delta > 1:
        return (
            f"promotion must climb exactly one rung: {from_stage!r} -> "
            f"{to_stage!r} skips {delta - 1}"
        )
    if delta == 1:
        evidence = transition.get("evidence_sha256")
        listed = (
            list(evidence)
            if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes))
            else []
        )
        if not listed:
            return (
                "promotion cites no evidence-record integrity hashes; every "
                "upward transition must name the records it rests on"
            )
        # Length alone is not citation: [""] and ["not-a-hash"] both satisfied
        # "names the records it rests on" while naming nothing. An evidence
        # pointer must be shaped like the ledger-line hash it claims to be.
        malformed = [item for item in listed if not EVIDENCE_SHA256_RE.fullmatch(str(item))]
        if malformed:
            return (
                "promotion cites malformed evidence: "
                + ", ".join(repr(str(item)) for item in malformed[:3])
                + " — every evidence_sha256 entry must be a 64-character "
                "lowercase-hex ledger-line integrity hash"
            )
        if to_stage == stages[-1] and not bool(policy.get("live_money_reachable")):
            return (
                f"the {stages[-1]!r} rung is unreachable under {policy_version}: "
                f"it opens only when a declared gate has passed twice on "
                f"schedule AND a new policy version declares it reachable"
            )
    return None


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
