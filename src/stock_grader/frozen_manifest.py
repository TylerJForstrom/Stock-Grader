"""Sibling manifests for score panels — the manifest convention IS the catalog.

The ecosystem's only registry is the per-directory ``manifest.json``
convention: ``staleness`` clocks derive from manifests, and the cross-repo
adapters (``FoundryDataSource``, ``VaultDataSource``) refuse unmanifested or
hash-mismatched bytes. Two boundary crossings bypassed that convention:

1. ``frozen_scores/<profile>/`` shipped bare parquet with no ``manifest.json``
   (recorded as a gap in Stock-Data's ECOSYSTEM.md decision log, 2026-08-03),
   so the vault's shadow arms consumed the forward evidence by bare path.
2. ``stock-grader backtest <panel.parquet>`` read whatever file it was handed
   without ever consulting the manifest its producer wrote beside it.

This module closes the grader's half of both: a writer the freeze and panel
builders call, and a verifier every panel-consuming loader calls. The writer
is the same ~50-line pattern as ``Stock-Data/src/stock_data/manifest.py`` and
``Stock-Vault/src/stock_vault/manifest.py`` — copied, never imported, per the
ecosystem's artifacts-not-imports rule — emitting the identical shape
(``schema_version`` 1.0, ``generated_at_utc``, ``source_urls``,
``license_note``, ``files`` of name/sha256/bytes) plus catalog fields the
plain datasets do not need:

- ``content_schema_versions`` (top level) and ``content_schema_version`` (per
  file): the ROW schema of the panels — the parquet ``schema_version``
  column — deliberately DISTINCT from the manifest FORMAT version in
  ``schema_version``. Both have always been "1.0", which is exactly why they
  must be separate fields: the day one bumps without the other, a shared
  field would make the bump invisible.
- per-file ``rows`` and ``columns`` (name -> pandas dtype): the schema block,
  informational catalog metadata. Integrity is the sha256; the columns block
  exists so a consumer can know a panel's shape without reading it.
- per-file ``hashed_at``: honest provenance. ``"freeze"``/``"build"`` only
  when the entry was first written by the same run that produced the bytes;
  ``"backfill"`` when the hash was computed after the fact. A backfilled hash
  pins the bytes from now on, but it cannot attest what happened before it
  was taken, and the catalog must not pretend otherwise.

Verification contract (the consuming half of the boundary):

- sibling ``manifest.json`` present and the file matches its sha256 -> read;
- no manifest -> warn (:class:`UnmanifestedPanelWarning`) and read: directories
  frozen before the convention are immutable — they are never rewritten — so
  they stay readable, but their bytes are attested by nothing;
- anything else -> ``ValueError``: hash mismatch, a file the manifest does not
  catalog, an unreadable manifest, or an unknown manifest ``schema_version``.
  Honest refusals over silent fallbacks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import warnings
from pathlib import Path

import pandas as pd

__all__ = [
    "FROZEN_LICENSE_NOTE",
    "MANIFEST_SCHEMA_VERSION",
    "RESTRICTED_BUILT_LICENSE_NOTE",
    "UnmanifestedPanelWarning",
    "refresh_built_panel_manifest",
    "refresh_frozen_manifest",
    "verify_sibling_manifest",
]

#: Manifest FORMAT version — the ecosystem-wide manifest.json contract, shared
#: with the foundry and vault datasets. NOT the version of the rows inside the
#: panels; that is ``content_schema_versions``.
MANIFEST_SCHEMA_VERSION = "1.0"

FROZEN_LICENSE_NOTE = (
    "Grader-computed scores derived from US-government public-domain SEC EDGAR "
    "data (17 USC 105); freely redistributable."
)

#: Built forward panels embed per-row returns derived from restricted sources
#: (Massive free-tier closes, stockanalysis.com delisted histories). The
#: manifest travels with the panel into gitignored/build or the private vault;
#: the note is the licensing wall stated where the bytes are.
RESTRICTED_BUILT_LICENSE_NOTE = (
    "Derived per-row returns from Massive (ex-Polygon) free-tier EOD closes and "
    "stockanalysis.com delisted histories; restricted — never commit these files "
    "to a public repository."
)

_DATE_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UnmanifestedPanelWarning(UserWarning):
    """A panel was read from a directory no manifest catalogs (pre-convention)."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prior_entries(manifest_path: Path) -> dict[str, dict]:
    """Entries of an existing manifest, for provenance carry-forward.

    NO manifest means a pre-convention directory: an empty carry-forward is
    correct, and every entry is honestly marked ``backfill``. A manifest that
    is PRESENT but unreadable, or written to a schema_version this code does
    not understand, is a different thing entirely — ``verify_sibling_manifest``
    refuses both — and returning ``{}`` for them made the producer destroy the
    catalog the consumer refuses and rebuild it over whatever bytes are on
    disk, silencing the alarm. Refuse instead: the producer may extend a
    catalog, never overwrite one it cannot read.
    """
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"{manifest_path} exists but cannot be parsed ({exc}); refusing to "
            f"overwrite a catalog this code cannot read — a corrupt manifest must "
            f"be investigated, never silently rebuilt"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != (
        MANIFEST_SCHEMA_VERSION
    ):
        version = manifest.get("schema_version") if isinstance(manifest, dict) else None
        raise ValueError(
            f"{manifest_path} declares manifest schema_version {version!r} "
            f"(supported: {MANIFEST_SCHEMA_VERSION}); refusing to downgrade a catalog "
            f"written by code that knows more than this does"
        )
    entries = {}
    for entry in manifest.get("files", []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            entries[entry["name"]] = entry
    return entries


def _file_entry(
    path: Path,
    prior: dict[str, dict],
    produced_now: frozenset[str],
    produced_label: str,
) -> dict:
    """One catalog entry. Identical bytes carry their prior catalog block —
    including ``hashed_at`` — forward unchanged; bytes this run produced are
    read and described afresh.

    Bytes that CHANGED under a name the catalog already attested, and that this
    run did not produce, are a refusal. ``verify_sibling_manifest`` calls that
    state "corruption or tampering, never a version bump" and raises; before
    this guard the writer re-hashed it, wrote the new digest with
    ``hashed_at: "backfill"``, and the next read verified clean — the producer
    laundering exactly what the consumer refuses. The freeze already goes red
    on an uncatalogable part, so this reuses that alarm path.
    """
    digest = _sha256(path)
    previous = prior.get(path.name)
    if path.name not in produced_now and previous is not None:
        recorded = previous.get("sha256")
        if isinstance(recorded, str) and recorded != digest:
            raise ValueError(
                f"{path} changed under a name the catalog already attests: the "
                f"manifest records {recorded}, the bytes hash to {digest}. Panels are "
                f"immutable — a changed part is corruption or tampering, never a "
                f"version bump; refusing to re-bless it. Restore the cataloged bytes "
                f"or retire the part under a new date."
            )
        if recorded == digest and previous.get("hashed_at") in ("freeze", "build", "backfill"):
            return dict(previous)
    entry: dict[str, object] = {
        "name": path.name,
        "sha256": digest,
        "bytes": path.stat().st_size,
        "hashed_at": produced_label if path.name in produced_now else "backfill",
    }
    if path.suffix.lower() == ".parquet":
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            raise ValueError(
                f"{path} cannot be read as parquet ({exc}); refusing to catalog "
                f"bytes that do not parse as a panel — a corrupt part must be "
                f"investigated, never blessed"
            ) from exc
        entry["rows"] = int(len(frame))
        entry["columns"] = {name: str(dtype) for name, dtype in frame.dtypes.items()}
        if "schema_version" in frame.columns:
            versions = sorted({str(v) for v in frame["schema_version"].dropna().unique()})
            if len(versions) > 1:
                raise ValueError(
                    f"{path} mixes row schema_version values {versions} inside one "
                    f"immutable part; refusing to catalog a malformed panel"
                )
            entry["content_schema_version"] = versions[0] if versions else None
        else:
            entry["content_schema_version"] = None
    return entry


def _write_if_changed(directory: Path, manifest: dict[str, object]) -> Path:
    """Write ``manifest.json`` atomically, unless only ``generated_at_utc``
    would change — identical content is never rewritten, so re-running a
    freeze that froze nothing leaves no diff."""
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            existing = None
        if isinstance(existing, dict):
            drop = "generated_at_utc"
            if {k: v for k, v in existing.items() if k != drop} == {
                k: v for k, v in manifest.items() if k != drop
            }:
                return manifest_path
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(manifest_path)
    return manifest_path


def _refresh(
    directory: Path,
    parts: list[Path],
    *,
    produced_now: frozenset[str],
    produced_label: str,
    license_note: str,
    source_urls: list[str],
    extra: dict[str, object],
) -> Path | None:
    if not parts:
        return None
    prior = _prior_entries(directory / "manifest.json")
    files = [_file_entry(path, prior, produced_now, produced_label) for path in parts]
    content_versions = sorted(
        {
            str(entry["content_schema_version"])
            for entry in files
            if entry.get("content_schema_version")
        }
    )
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_urls": sorted(source_urls),
        "license_note": license_note,
        "content_schema_versions": content_versions,
        "files": files,
    }
    manifest.update(extra)
    return _write_if_changed(directory, manifest)


def refresh_frozen_manifest(
    profile_dir: Path | str, *, frozen_now: frozenset[str] = frozenset()
) -> Path | None:
    """(Re)write ``frozen_scores/<profile>/manifest.json`` over the date parts.

    Catalogs exactly the files the readers accept — ``YYYY-MM-DD.parquet``
    (the same stem discipline ``discover_frozen_panels`` and the paper trader
    apply) — so a stray temp file can neither poison the catalog nor borrow
    its blessing. ``frozen_now`` names files this very run froze; everything
    else keeps its prior provenance or is honestly marked ``backfill``.
    Additive only: panels are immutable and are never rewritten — the manifest
    beside them is the catalog, extended as parts accrue. Returns the manifest
    path, or None when the directory holds no date parts.
    """
    profile_dir = Path(profile_dir)
    parts = [
        path for path in sorted(profile_dir.glob("*.parquet")) if _DATE_STEM.fullmatch(path.stem)
    ]
    return _refresh(
        profile_dir,
        parts,
        produced_now=frozen_now,
        produced_label="freeze",
        license_note=FROZEN_LICENSE_NOTE,
        source_urls=["https://www.sec.gov/"],
        extra={"dataset": "frozen_score_panel", "profile": profile_dir.name},
    )


def refresh_built_panel_manifest(
    out_dir: Path | str, *, built_now: frozenset[str] = frozenset()
) -> Path | None:
    """(Re)write ``manifest.json`` beside built forward panels and sidecars.

    ``build-panel`` writes ``<profile>.parquet`` + ``<profile>.build.json``
    into a shared output directory and ``backtest`` consumes the parquet by
    path; this manifest is what lets that consumption verify instead of
    trust. Catalogs every ``*.parquet`` and ``*.build.json`` in the
    directory. Returns the manifest path, or None when there is nothing to
    catalog.
    """
    out_dir = Path(out_dir)
    parts = sorted(
        path
        for path in out_dir.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and (path.suffix.lower() == ".parquet" or path.name.endswith(".build.json"))
    )
    return _refresh(
        out_dir,
        parts,
        produced_now=built_now,
        produced_label="build",
        license_note=RESTRICTED_BUILT_LICENSE_NOTE,
        source_urls=["https://massive.dev/", "https://stockanalysis.com/"],
        extra={"dataset": "built_forward_panel"},
    )


def verify_sibling_manifest(path: Path | str, *, strict: bool = False) -> bool:
    """Verify one panel file against the ``manifest.json`` beside it.

    Returns True when the sibling manifest catalogs the file and its sha256
    matches; False — after an :class:`UnmanifestedPanelWarning` — when the
    directory has no manifest at all (pre-convention directories are
    immutable and stay readable, but nothing attests their bytes). Raises
    ``ValueError`` for everything in between: an unreadable manifest, an
    unknown manifest ``schema_version``, a file the manifest does not list,
    or a sha256 mismatch. Panels are immutable, so a changed part is
    corruption or tampering, never a version bump — the only honest response
    is refusal.

    ``strict=True`` turns the missing-manifest case into the same refusal. Use
    it for any directory whose producer catalogs by contract — the committed
    evidence roots and ``build/panels`` — because there the escape hatch is not
    a pre-convention directory, it is a deleted file: without it, the strongest
    control in the pipeline is defeated by removing one path instead of by
    editing a hash, and the warning it degrades to is invisible in CI.
    """
    path = Path(path)
    manifest_path = path.parent / "manifest.json"
    if not manifest_path.is_file():
        if strict:
            raise ValueError(
                f"{path} has no sibling manifest.json and this read requires "
                f"attestation: the producer of this directory catalogs every part it "
                f"writes, so an absent catalog is a deleted or never-written one, not "
                f"a pre-convention directory. Refusing to treat unattested bytes as "
                f"attested."
            )
        warnings.warn(
            f"{path} has no sibling manifest.json: nothing attests these bytes. "
            f"Directories that predate the manifest convention stay readable, but "
            f"new panels must be cataloged by their producer.",
            UnmanifestedPanelWarning,
            stacklevel=2,
        )
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"{manifest_path} cannot be parsed ({exc}); refusing to read "
            f"{path.name} against a catalog that cannot vouch for it"
        ) from exc
    version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"unknown manifest schema_version {version!r} in {manifest_path} "
            f"(supported: {MANIFEST_SCHEMA_VERSION}); refusing to verify "
            f"{path.name} against a contract this code does not understand"
        )
    entries = {
        entry.get("name"): entry for entry in manifest.get("files", []) if isinstance(entry, dict)
    }
    entry = entries.get(path.name)
    if entry is None:
        raise ValueError(
            f"{path.name} is not cataloged in {manifest_path}; a part the "
            f"manifest does not know is indistinguishable from tampering — "
            f"refusing to read it"
        )
    expected = entry.get("sha256")
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(
            f"sha256 mismatch for {path}: the manifest records {expected}, the "
            f"bytes hash to {actual}. Panels are immutable — a changed part is "
            f"corruption or tampering, never a version bump; refusing to read it"
        )
    return True
