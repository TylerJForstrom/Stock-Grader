"""Shared ticker-spelling helpers. Canonical form = SEC dash form.

Class shares are written differently across the live symbologies: EDGAR's
ticker map uses ``BRK-B`` (the ecosystem's canonical form — see Stock-Data's
ECOSYSTEM.md), Polygon and most humans write ``BRK.B``, and IB writes
``BRK B``. Every lookup that keys on a ticker must try all spellings through
this one helper — per-module ad-hoc variants are how the insider-price path
silently lost dot-form tickers and how VaultDataSource grew two private
copies of the space form.

FINRA's no-separator form (``BRKB``) is deliberately NOT produced here: a
squashed class share can spell a different issuer's real ticker, so joins
against no-separator sources must go through an ambiguity-guarded index
(Stock-Vault's ``build_squash_index``), never blind variant expansion.

Stock-Data and Stock-Vault carry their own copies of this helper (the
no-cross-repo-imports rule); all three must agree that canonical is the
dash form.
"""

from __future__ import annotations


def canonical_ticker(ticker: str) -> str:
    """Canonical SEC dash form: ``BRK.B``, ``BRK B``, and ``brk-b`` → ``BRK-B``."""
    text = ticker.upper().strip().replace(".", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text


def ticker_variants(ticker: str) -> tuple[str, ...]:
    """Ordered, de-duplicated spellings to try: as-given, dash, dot, space forms."""
    upper = ticker.upper().strip()
    canonical = canonical_ticker(upper)
    variants = [
        upper,
        canonical,
        canonical.replace("-", "."),
        canonical.replace("-", " "),
    ]
    seen: set[str] = set()
    ordered = []
    for candidate in variants:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)
