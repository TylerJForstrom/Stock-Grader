"""Synthetic price generation — for offline testing and for grading when no price feed is reachable.

No free price source was reachable from the build environment: Yahoo returns HTTP 429 on every
attempt and Stooq serves a JavaScript proof-of-work bot check rather than CSV
(docs/design/DATA-GROUND-TRUTH.md §1). Fundamentals come from SEC EDGAR, but the statistical,
risk and momentum pillars need a price series.

Two honest responses, both provided here:

* **Fixtures.** Generate a reproducible series with a *known* factor structure so the statistical
  layer can be unit-tested — a weighting method that cannot recover a planted signal is broken, and
  that is only testable against data whose truth you control.
* **Loud labelling.** Every snapshot built on generated prices carries ``meta["synthetic_prices"]``,
  which the report renders as a visible banner. Generated data must never be mistaken for market
  history, so no function here is reachable without passing ``synthetic=True`` explicitly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["SYNTHETIC_BANNER", "generate_panel", "generate_prices"]

SYNTHETIC_BANNER = (
    "SYNTHETIC PRICE DATA — generated, not real market history. "
    "Risk, momentum and statistical pillars are illustrative only."
)

_TRADING_DAYS = 252


def generate_prices(
    ticker: str,
    *,
    n_days: int = 1260,
    start_price: float = 100.0,
    annual_drift: float = 0.08,
    annual_vol: float = 0.25,
    seed: int | None = None,
    jump_intensity: float = 0.02,
    jump_scale: float = 0.05,
    vol_of_vol: float = 0.3,
    end: pd.Timestamp | str | None = None,
    synthetic: bool = False,
) -> pd.DataFrame:
    """Generate an OHLCV series with realistic stylised facts.

    A plain geometric Brownian motion is a poor test fixture: it has no fat tails, no volatility
    clustering and no drawdown structure, so estimators like the Hill tail index, GARCH persistence
    and the variance-ratio test would all be exercised against data that cannot distinguish a
    working implementation from a broken one. This uses a GARCH-like stochastic volatility with
    Poisson jumps, which produces excess kurtosis and vol clustering.

    Args:
        synthetic: must be ``True``. Present so no caller can produce generated prices by accident.
    """
    if not synthetic:
        raise ValueError(
            "generate_prices produces fabricated data; pass synthetic=True to acknowledge that "
            "the output is not real market history"
        )
    if n_days < 2:
        raise ValueError("n_days must be at least 2")

    # Seed deterministically from the ticker so a given name always yields the same series.
    if seed is None:
        seed = abs(hash(ticker)) % (2**31)
    rng = np.random.default_rng(seed)

    dt = 1.0 / _TRADING_DAYS
    mu = annual_drift * dt
    base_var = (annual_vol**2) * dt

    # Stochastic volatility: log-variance follows an OU process -> clustering.
    log_var = np.empty(n_days)
    log_var[0] = np.log(base_var)
    kappa, theta = 0.05, np.log(base_var)
    shocks = rng.standard_normal(n_days)
    for t in range(1, n_days):
        log_var[t] = log_var[t - 1] + kappa * (theta - log_var[t - 1]) + vol_of_vol * np.sqrt(dt) * shocks[t]
    var = np.exp(log_var)

    returns = mu + np.sqrt(var) * rng.standard_normal(n_days)

    # Poisson jumps -> fat tails.
    n_jumps = rng.poisson(jump_intensity * n_days)
    if n_jumps:
        idx = rng.choice(n_days, size=n_jumps, replace=False)
        returns[idx] += rng.standard_normal(n_jumps) * jump_scale

    close = start_price * np.exp(np.cumsum(returns))

    end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=end_ts, periods=n_days)

    intraday = np.abs(rng.standard_normal(n_days)) * np.sqrt(var) * 0.5
    open_ = close * (1 + rng.standard_normal(n_days) * np.sqrt(var) * 0.3)
    high = np.maximum(open_, close) * (1 + intraday)
    low = np.minimum(open_, close) * (1 - intraday)
    volume = rng.lognormal(mean=15.0, sigma=0.4, size=n_days).round()

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "adj_close": close,
            "volume": volume,
        },
        index=dates,
    )


def generate_panel(
    tickers: list[str],
    *,
    n_days: int = 1260,
    seed: int = 0,
    market_beta_spread: tuple[float, float] = (0.5, 1.6),
    planted_factor: np.ndarray | None = None,
    factor_strength: float = 0.0,
    synthetic: bool = False,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.Series]:
    """Generate a correlated cross-section sharing a market factor.

    Returns ``(prices_by_ticker, benchmark, planted_exposure)``. When ``planted_factor`` is
    supplied, each name's drift is tilted by ``factor_strength * exposure``, giving the validation
    harness a ground truth: a weighting method that cannot recover a planted signal from this panel
    is broken, and that is the only way to test the weighting layer honestly.
    """
    if not synthetic:
        raise ValueError("generate_panel produces fabricated data; pass synthetic=True")

    rng = np.random.default_rng(seed)
    n = len(tickers)
    betas = rng.uniform(*market_beta_spread, size=n)
    exposure = planted_factor if planted_factor is not None else rng.standard_normal(n)

    market = generate_prices(
        "__MARKET__", n_days=n_days, annual_drift=0.07, annual_vol=0.16, seed=seed, synthetic=True
    )
    market_returns = np.log(market["close"]).diff().fillna(0.0).to_numpy()

    prices: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(tickers):
        idio = generate_prices(
            ticker,
            n_days=n_days,
            annual_drift=factor_strength * float(exposure[i]),
            annual_vol=0.22,
            seed=seed + i + 1,
            synthetic=True,
        )
        idio_returns = np.log(idio["close"]).diff().fillna(0.0).to_numpy()
        combined = betas[i] * market_returns + idio_returns
        close = 100.0 * np.exp(np.cumsum(combined))
        frame = idio.copy()
        scale = close / frame["close"].to_numpy()
        for col in ("open", "high", "low", "close", "adj_close"):
            frame[col] = frame[col].to_numpy() * scale
        prices[ticker] = frame

    return prices, market, pd.Series(exposure, index=tickers, name="planted_exposure")
