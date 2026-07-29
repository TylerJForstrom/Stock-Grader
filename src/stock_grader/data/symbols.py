"""Shared ticker-spelling helpers.

Class shares are written differently across SEC surfaces: EDGAR's ticker map
uses ``BRK-B`` while humans (and some SEC datasets) write ``BRK.B``. Every
lookup that keys on a ticker must try both spellings through this one helper —
per-module ad-hoc variants are how the insider-price path silently lost
dot-form tickers.
"""

from __future__ import annotations


def ticker_variants(ticker: str) -> tuple[str, ...]:
    """Ordered, de-duplicated spellings to try: as-given, dot→dash, dash→dot."""
    upper = ticker.upper().strip()
    variants = [upper, upper.replace(".", "-"), upper.replace("-", ".")]
    seen: set[str] = set()
    ordered = []
    for candidate in variants:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)
