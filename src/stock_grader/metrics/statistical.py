"""Statistical, risk, momentum and time-series metrics.

The quantitative half of the grade. Everything here is computed from the adjusted-close series, and
several conventions are fixed once so they cannot drift between metrics:

* **252 trading days** per year for every annualisation.
* **Log returns** for compounding and statistical work, simple returns only where a metric is
  defined on them.
* **Risk-free rate is subtracted properly.** A Sharpe ratio against an assumed 0% risk-free rate is
  a different statistic, and a flattering one whenever rates are positive. The rate comes from FRED
  (verified reachable) and is converted from annualised to daily before subtraction.
* **Insufficient history returns ``None``.** A "5-year beta" from 200 days is not a 5-year beta.
  Each metric declares ``min_history`` and the engine enforces it before the function is called.

Some metrics are deliberately **non-monotonic** (``direction=0``): both extremes are bad and there
is an ideal band. Beta is the clearest case — 0.2 and 2.5 are both awkward, for opposite reasons —
and forcing it through a monotonic ranking would silently assert that less beta is always better.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from ..registry import metric
from ..types import SecuritySnapshot
from .util import safe_div

TRADING_DAYS = 252
_MIN_FOR_STATS = 60


def _prices(s: SecuritySnapshot) -> pd.Series | None:
    if not s.has_prices:
        return None
    frame = s.prices
    column = "adj_close" if "adj_close" in frame.columns else "close"
    series = frame[column].dropna()
    return series if len(series) >= 2 else None


def _log_returns(s: SecuritySnapshot, window: int | None = None) -> pd.Series | None:
    prices = _prices(s)
    if prices is None:
        return None
    returns = np.log(prices / prices.shift(1)).dropna()
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if window is not None:
        returns = returns.iloc[-window:]
    return returns if len(returns) >= 2 else None


def _daily_risk_free(s: SecuritySnapshot, index: pd.Index) -> pd.Series:
    """Daily risk-free rate aligned to a return index; zero when unavailable."""
    if s.risk_free is None or s.risk_free.empty:
        return pd.Series(0.0, index=index)
    aligned = s.risk_free.reindex(index.union(s.risk_free.index)).sort_index()
    aligned = aligned.ffill().reindex(index).fillna(0.0)
    return aligned / TRADING_DAYS


def _excess(s: SecuritySnapshot, returns: pd.Series) -> pd.Series:
    return returns - _daily_risk_free(s, returns.index)


def _benchmark_returns(s: SecuritySnapshot, index: pd.Index) -> pd.Series | None:
    if s.benchmark is None or s.benchmark.empty:
        return None
    column = "adj_close" if "adj_close" in s.benchmark.columns else "close"
    prices = s.benchmark[column].dropna()
    returns = np.log(prices / prices.shift(1)).dropna()
    aligned = returns.reindex(index).dropna()
    return aligned if len(aligned) >= 30 else None


# ---------------------------------------------------------------------------------------------
# Return and risk
# ---------------------------------------------------------------------------------------------


@metric("annualized_return_1y", pillar="risk", direction=1, unit="ratio",
        needs_prices=True, min_history=252, winsor=(-1.0, 5.0))
def annualized_return_1y(s: SecuritySnapshot) -> float | None:
    """Annualised log return over the last year."""
    returns = _log_returns(s, TRADING_DAYS)
    return float(returns.mean() * TRADING_DAYS) if returns is not None else None


@metric("annualized_volatility", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=126)
def annualized_volatility(s: SecuritySnapshot) -> float | None:
    """Annualised standard deviation of daily log returns."""
    returns = _log_returns(s, TRADING_DAYS)
    return float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if returns is not None else None


@metric("downside_deviation", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=126)
def downside_deviation(s: SecuritySnapshot) -> float | None:
    """Annualised deviation of negative returns only — volatility that actually hurts."""
    returns = _log_returns(s, TRADING_DAYS)
    if returns is None:
        return None
    negative = returns[returns < 0]
    if len(negative) < 5:
        return None
    return float(np.sqrt((negative**2).mean()) * np.sqrt(TRADING_DAYS))


@metric("sharpe_ratio", pillar="risk", direction=1, unit="ratio",
        needs_prices=True, min_history=252, winsor=(-5.0, 5.0))
def sharpe_ratio(s: SecuritySnapshot) -> float | None:
    """Excess return per unit of total volatility, against the real risk-free rate."""
    returns = _log_returns(s, TRADING_DAYS)
    if returns is None:
        return None
    excess = _excess(s, returns)
    sigma = float(excess.std(ddof=1))
    if sigma <= 0:
        return None
    return float(excess.mean() / sigma * np.sqrt(TRADING_DAYS))


@metric("sortino_ratio", pillar="risk", direction=1, unit="ratio",
        needs_prices=True, min_history=252, winsor=(-5.0, 10.0))
def sortino_ratio(s: SecuritySnapshot) -> float | None:
    """Excess return per unit of downside deviation."""
    returns = _log_returns(s, TRADING_DAYS)
    if returns is None:
        return None
    excess = _excess(s, returns)
    negative = excess[excess < 0]
    if len(negative) < 5:
        return None
    downside = float(np.sqrt((negative**2).mean()))
    if downside <= 0:
        return None
    return float(excess.mean() / downside * np.sqrt(TRADING_DAYS))


@metric("max_drawdown", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=252)
def max_drawdown(s: SecuritySnapshot) -> float | None:
    """Deepest peak-to-trough decline, as a positive fraction."""
    prices = _prices(s)
    if prices is None or len(prices) < 60:
        return None
    drawdown = prices / prices.cummax() - 1.0
    return float(-drawdown.min())


@metric("calmar_ratio", pillar="risk", direction=1, unit="ratio",
        needs_prices=True, min_history=756, winsor=(-10.0, 10.0))
def calmar_ratio(s: SecuritySnapshot) -> float | None:
    """Three-year annualised return divided by max drawdown."""
    returns = _log_returns(s, TRADING_DAYS * 3)
    drawdown = max_drawdown.fn(s)
    if returns is None or drawdown is None or drawdown <= 0:
        return None
    return safe_div(float(returns.mean() * TRADING_DAYS), drawdown, positive_denominator=True, cap=10.0)


@metric("ulcer_index", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=252)
def ulcer_index(s: SecuritySnapshot) -> float | None:
    """Root-mean-square drawdown — penalises deep *and* long declines, unlike max drawdown."""
    prices = _prices(s)
    if prices is None or len(prices) < 60:
        return None
    drawdown = (prices / prices.cummax() - 1.0) * 100.0
    return float(np.sqrt((drawdown**2).mean()))


@metric("var_95", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=252)
def var_95(s: SecuritySnapshot) -> float | None:
    """Historical 95% one-day value at risk, as a positive loss magnitude."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None or len(returns) < 100:
        return None
    return float(-np.percentile(returns, 5))


@metric("cvar_95", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=252)
def cvar_95(s: SecuritySnapshot) -> float | None:
    """Expected shortfall: the mean loss *given* the worst 5% of days.

    More informative than VaR, which says nothing about how bad the tail gets beyond its threshold.
    """
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None or len(returns) < 100:
        return None
    threshold = np.percentile(returns, 5)
    tail = returns[returns <= threshold]
    return float(-tail.mean()) if len(tail) else None


@metric("cornish_fisher_var", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=252)
def cornish_fisher_var(s: SecuritySnapshot) -> float | None:
    """VaR adjusted for skew and kurtosis via the Cornish-Fisher expansion.

    Gaussian VaR understates risk for the fat-tailed, left-skewed distributions equities actually
    have; this corrects the quantile using the sample's own third and fourth moments.
    """
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None or len(returns) < 100:
        return None
    mu, sigma = float(returns.mean()), float(returns.std(ddof=1))
    if sigma <= 0:
        return None
    skew, kurt = float(stats.skew(returns)), float(stats.kurtosis(returns))
    z = stats.norm.ppf(0.05)
    adjusted = (
        z
        + (z**2 - 1) * skew / 6.0
        + (z**3 - 3 * z) * kurt / 24.0
        - (2 * z**3 - 5 * z) * (skew**2) / 36.0
    )
    return float(-(mu + adjusted * sigma))


@metric("return_skew", pillar="risk", direction=1, unit="moment",
        needs_prices=True, min_history=252, winsor=(-5.0, 5.0))
def return_skew(s: SecuritySnapshot) -> float | None:
    """Skewness of daily returns. Positive is preferable — big up days, small down days."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    return float(stats.skew(returns)) if returns is not None and len(returns) >= 100 else None


@metric("excess_kurtosis", pillar="risk", direction=-1, unit="moment",
        needs_prices=True, min_history=252, winsor=(-5.0, 30.0))
def excess_kurtosis(s: SecuritySnapshot) -> float | None:
    """Excess kurtosis — how fat the tails are relative to a normal distribution."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    return float(stats.kurtosis(returns)) if returns is not None and len(returns) >= 100 else None


@metric("tail_ratio", pillar="risk", direction=1, unit="ratio",
        needs_prices=True, min_history=252, winsor=(0.0, 5.0))
def tail_ratio(s: SecuritySnapshot) -> float | None:
    """95th percentile gain divided by the magnitude of the 5th percentile loss."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None or len(returns) < 100:
        return None
    upper, lower = np.percentile(returns, 95), abs(np.percentile(returns, 5))
    return safe_div(upper, lower, positive_denominator=True, cap=5.0)


@metric("hill_tail_index", pillar="risk", direction=1, unit="index",
        needs_prices=True, min_history=504, winsor=(0.0, 10.0))
def hill_tail_index(s: SecuritySnapshot) -> float | None:
    """Hill estimator of the left-tail index. Higher means thinner (safer) tails.

    Fitted on the worst 10% of losses. A lower index implies a heavier tail and a materially higher
    probability of extreme loss than a normal model would suggest.
    """
    returns = _log_returns(s, TRADING_DAYS * 3)
    if returns is None or len(returns) < 250:
        return None
    losses = -returns[returns < 0]
    if len(losses) < 50:
        return None
    ordered = np.sort(losses.to_numpy())[::-1]
    k = max(10, int(len(ordered) * 0.10))
    k = min(k, len(ordered) - 1)
    threshold = ordered[k]
    if threshold <= 0:
        return None
    top = ordered[:k]
    top = top[top > 0]
    if len(top) < 5:
        return None
    hill = float(np.mean(np.log(top / threshold)))
    return 1.0 / hill if hill > 0 else None


# ---------------------------------------------------------------------------------------------
# Factor exposure
# ---------------------------------------------------------------------------------------------


@metric("beta", pillar="risk", direction=0, unit="beta", ideal_band=(0.7, 1.15),
        needs_prices=True, needs_benchmark=True, min_history=252, winsor=(-3.0, 5.0))
def beta(s: SecuritySnapshot) -> float | None:
    """CAPM beta versus the benchmark — **non-monotonic**.

    Neither extreme is desirable: a beta of 0.2 means the stock barely participates in market
    returns, a beta of 2.5 means it amplifies every drawdown. The ideal band sits slightly below 1,
    and scoring this monotonically would assert that the lowest-beta stock is always the best one.
    """
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None:
        return None
    market = _benchmark_returns(s, returns.index)
    if market is None:
        return None
    aligned = pd.concat([returns.rename("r"), market.rename("m")], axis=1).dropna()
    if len(aligned) < 60:
        return None
    variance = float(aligned["m"].var(ddof=1))
    if variance <= 0:
        return None
    return float(aligned["r"].cov(aligned["m"]) / variance)


@metric("capm_alpha", pillar="risk", direction=1, unit="ratio",
        needs_prices=True, needs_benchmark=True, min_history=252, winsor=(-2.0, 2.0))
def capm_alpha(s: SecuritySnapshot) -> float | None:
    """Annualised CAPM intercept — return unexplained by market exposure."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None:
        return None
    market = _benchmark_returns(s, returns.index)
    if market is None:
        return None
    aligned = pd.concat([returns.rename("r"), market.rename("m")], axis=1).dropna()
    if len(aligned) < 60:
        return None
    rf = _daily_risk_free(s, aligned.index)
    y = (aligned["r"] - rf).to_numpy()
    x = (aligned["m"] - rf).to_numpy()
    variance = float(np.var(x, ddof=1))
    if variance <= 0:
        return None
    slope = float(np.cov(y, x, ddof=1)[0, 1] / variance)
    return float((y.mean() - slope * x.mean()) * TRADING_DAYS)


@metric("idiosyncratic_volatility", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, needs_benchmark=True, min_history=252)
def idiosyncratic_volatility(s: SecuritySnapshot) -> float | None:
    """Annualised volatility of CAPM residuals — risk that diversification does not remove."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None:
        return None
    market = _benchmark_returns(s, returns.index)
    if market is None:
        return None
    aligned = pd.concat([returns.rename("r"), market.rename("m")], axis=1).dropna()
    if len(aligned) < 60:
        return None
    x = aligned["m"].to_numpy()
    y = aligned["r"].to_numpy()
    variance = float(np.var(x, ddof=1))
    if variance <= 0:
        return None
    slope = float(np.cov(y, x, ddof=1)[0, 1] / variance)
    residuals = y - (y.mean() - slope * x.mean()) - slope * x
    return float(np.std(residuals, ddof=1) * np.sqrt(TRADING_DAYS))


# ---------------------------------------------------------------------------------------------
# Time-series structure
# ---------------------------------------------------------------------------------------------


@metric("hurst_exponent", pillar="risk", direction=0, unit="exponent", ideal_band=(0.45, 0.60),
        needs_prices=True, min_history=504)
def hurst_exponent(s: SecuritySnapshot) -> float | None:
    """Hurst exponent by rescaled-range analysis — **non-monotonic**.

    ``H = 0.5`` is a random walk, ``H > 0.5`` trending, ``H < 0.5`` mean-reverting. Neither
    extreme is straightforwardly good, so this is scored through a band around the random-walk
    value rather than ranked. Note R/S and DFA estimators routinely disagree on real price series;
    this is the R/S estimate and should be read as indicative.
    """
    returns = _log_returns(s, TRADING_DAYS * 3)
    if returns is None or len(returns) < 250:
        return None
    series = returns.to_numpy()
    n = len(series)
    scales = [s_ for s_ in (8, 16, 32, 64, 128, 256) if s_ <= n // 4]
    if len(scales) < 3:
        return None
    rs_values = []
    for scale in scales:
        chunks = n // scale
        ratios = []
        for i in range(chunks):
            chunk = series[i * scale : (i + 1) * scale]
            deviations = np.cumsum(chunk - chunk.mean())
            spread = deviations.max() - deviations.min()
            sigma = chunk.std(ddof=1)
            if sigma > 0 and spread > 0:
                ratios.append(spread / sigma)
        if ratios:
            rs_values.append((scale, float(np.mean(ratios))))
    if len(rs_values) < 3:
        return None
    logs = np.log([v[0] for v in rs_values])
    log_rs = np.log([v[1] for v in rs_values])
    slope = float(np.polyfit(logs, log_rs, 1)[0])
    return slope if 0.0 <= slope <= 1.5 else None


@metric("variance_ratio", pillar="risk", direction=0, unit="ratio", ideal_band=(0.9, 1.1),
        needs_prices=True, min_history=504)
def variance_ratio(s: SecuritySnapshot) -> float | None:
    """Lo-MacKinlay variance ratio at lag 5 — **non-monotonic**, 1.0 means a random walk.

    Above 1 indicates positive autocorrelation (trending), below 1 mean reversion.
    """
    returns = _log_returns(s, TRADING_DAYS * 3)
    if returns is None or len(returns) < 250:
        return None
    series = returns.to_numpy()
    q = 5
    n = len(series) - (len(series) % q)
    if n < 50:
        return None
    series = series[:n]
    var_1 = float(np.var(series, ddof=1))
    if var_1 <= 0:
        return None
    aggregated = series.reshape(-1, q).sum(axis=1)
    var_q = float(np.var(aggregated, ddof=1))
    return float(var_q / (q * var_1))


@metric("return_autocorrelation", pillar="risk", direction=0, unit="rho", ideal_band=(-0.05, 0.05),
        needs_prices=True, min_history=252)
def return_autocorrelation(s: SecuritySnapshot) -> float | None:
    """Lag-1 autocorrelation of daily returns — **non-monotonic**, near zero is normal."""
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None or len(returns) < 100:
        return None
    value = returns.autocorr(1)
    return float(value) if value is not None and np.isfinite(value) else None


@metric("mean_reversion_half_life", pillar="risk", direction=1, unit="days",
        needs_prices=True, min_history=504, winsor=(0.0, 500.0))
def mean_reversion_half_life(s: SecuritySnapshot) -> float | None:
    """Half-life of mean reversion from an Ornstein-Uhlenbeck fit to log price."""
    prices = _prices(s)
    if prices is None or len(prices) < 250:
        return None
    log_price = np.log(prices.iloc[-TRADING_DAYS * 3 :])
    lagged = log_price.shift(1).dropna()
    delta = (log_price - log_price.shift(1)).dropna()
    aligned = pd.concat([delta.rename("d"), lagged.rename("l")], axis=1).dropna()
    if len(aligned) < 100:
        return None
    x = aligned["l"].to_numpy()
    variance = float(np.var(x, ddof=1))
    if variance <= 0:
        return None
    theta = float(np.cov(aligned["d"].to_numpy(), x, ddof=1)[0, 1] / variance)
    if theta >= 0:
        return None  # not mean-reverting; the concept does not apply
    return float(-np.log(2) / theta)


@metric("volatility_of_volatility", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=378)
def volatility_of_volatility(s: SecuritySnapshot) -> float | None:
    """Standard deviation of rolling 21-day volatility — is the risk level itself stable?"""
    returns = _log_returns(s, TRADING_DAYS * 2)
    if returns is None or len(returns) < 150:
        return None
    rolling = returns.rolling(21).std(ddof=1).dropna()
    if len(rolling) < 30 or rolling.mean() <= 0:
        return None
    return float(rolling.std(ddof=1) / rolling.mean())


# ---------------------------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------------------------


def _total_return(s: SecuritySnapshot, days: int, skip: int = 0) -> float | None:
    prices = _prices(s)
    if prices is None or len(prices) < days + skip + 1:
        return None
    end = prices.iloc[-1 - skip]
    start = prices.iloc[-1 - skip - days]
    if start <= 0:
        return None
    return float(end / start - 1.0)


@metric("momentum_1m", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=63, winsor=(-1.0, 3.0))
def momentum_1m(s: SecuritySnapshot) -> float | None:
    """One-month total return."""
    return _total_return(s, 21)


@metric("momentum_3m", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=84, winsor=(-1.0, 5.0))
def momentum_3m(s: SecuritySnapshot) -> float | None:
    """Three-month total return."""
    return _total_return(s, 63)


@metric("momentum_6m", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=147, winsor=(-1.0, 5.0))
def momentum_6m(s: SecuritySnapshot) -> float | None:
    """Six-month total return."""
    return _total_return(s, 126)


@metric("momentum_12_1", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=273, winsor=(-1.0, 5.0))
def momentum_12_1(s: SecuritySnapshot) -> float | None:
    """Twelve-month return skipping the most recent month — the standard momentum factor.

    The skip matters: the most recent month exhibits short-term *reversal*, so including it
    contaminates the momentum signal with its opposite.
    """
    return _total_return(s, 231, skip=21)


@metric("risk_adjusted_momentum", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=273, winsor=(-10.0, 10.0))
def risk_adjusted_momentum(s: SecuritySnapshot) -> float | None:
    """12-1 momentum divided by annualised volatility."""
    momentum = momentum_12_1.fn(s)
    volatility = annualized_volatility.fn(s)
    if momentum is None or volatility is None or volatility <= 0:
        return None
    return float(momentum / volatility)


@metric("momentum_consistency", pillar="momentum", direction=1, unit="fraction",
        needs_prices=True, min_history=273)
def momentum_consistency(s: SecuritySnapshot) -> float | None:
    """How many of the 1/3/6/12-month horizons are positive — agreement across timeframes."""
    values = [_total_return(s, d) for d in (21, 63, 126, 252)]
    present = [v for v in values if v is not None]
    if len(present) < 3:
        return None
    return float(sum(1 for v in present if v > 0) / len(present))


@metric("information_discreteness", pillar="momentum", direction=-1, unit="ratio",
        needs_prices=True, min_history=273, winsor=(-1.0, 1.0))
def information_discreteness(s: SecuritySnapshot) -> float | None:
    """Frog-in-the-Pan: ``sign(PRET) * (%neg - %pos)`` over the momentum window.

    Momentum that arrived through many small moves persists better than the same total return
    delivered in a few jumps, because gradual information is underreacted to. Lower is better.
    """
    returns = _log_returns(s, TRADING_DAYS)
    momentum = momentum_12_1.fn(s)
    if returns is None or momentum is None or len(returns) < 100:
        return None
    positive = float((returns > 0).mean())
    negative = float((returns < 0).mean())
    return float(np.sign(momentum) * (negative - positive))


@metric("pct_positive_days", pillar="momentum", direction=1, unit="fraction",
        needs_prices=True, min_history=252)
def pct_positive_days(s: SecuritySnapshot) -> float | None:
    """Fraction of up days over the past year."""
    returns = _log_returns(s, TRADING_DAYS)
    return float((returns > 0).mean()) if returns is not None else None


@metric("distance_from_52w_high", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=252)
def distance_from_52w_high(s: SecuritySnapshot) -> float | None:
    """Current price relative to the 52-week high — proximity to the high predicts continuation."""
    prices = _prices(s)
    if prices is None or len(prices) < 200:
        return None
    window = prices.iloc[-TRADING_DAYS:]
    high = float(window.max())
    return safe_div(float(window.iloc[-1]), high, positive_denominator=True)


@metric("trend_strength", pillar="momentum", direction=1, unit="t-stat",
        needs_prices=True, min_history=252, winsor=(-50.0, 50.0))
def trend_strength(s: SecuritySnapshot) -> float | None:
    """t-statistic of the slope of a linear regression through log price.

    Distinguishes a steady climb from a volatile one that happens to end higher — the t-statistic
    penalises dispersion around the trend, which a plain return does not.
    """
    prices = _prices(s)
    if prices is None or len(prices) < 200:
        return None
    window = np.log(prices.iloc[-TRADING_DAYS:].to_numpy())
    n = len(window)
    x = np.arange(n, dtype="float64")
    x_centered = x - x.mean()
    denominator = float((x_centered**2).sum())
    if denominator <= 0:
        return None
    slope = float((x_centered * (window - window.mean())).sum() / denominator)
    residuals = window - (window.mean() - slope * x.mean() + slope * x)
    dof = n - 2
    sse = float((residuals**2).sum())
    if dof <= 0 or sse <= 0:
        return None
    se = np.sqrt(sse / dof / denominator)
    return float(slope / se) if se > 0 else None


# ---------------------------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------------------------


@metric("dollar_volume", pillar="liquidity", direction=1, unit="usd",
        needs_prices=True, min_history=63)
def dollar_volume(s: SecuritySnapshot) -> float | None:
    """Median daily dollar volume over the last quarter."""
    if not s.has_prices or "volume" not in s.prices.columns:
        return None
    frame = s.prices.iloc[-63:]
    column = "adj_close" if "adj_close" in frame.columns else "close"
    product = (frame[column] * frame["volume"]).dropna()
    return float(product.median()) if len(product) >= 20 else None


@metric("amihud_illiquidity", pillar="liquidity", direction=-1, unit="ratio",
        needs_prices=True, min_history=126)
def amihud_illiquidity(s: SecuritySnapshot) -> float | None:
    """Amihud measure: mean of ``|return| / dollar volume`` — price impact per dollar traded."""
    if not s.has_prices or "volume" not in s.prices.columns:
        return None
    frame = s.prices.iloc[-TRADING_DAYS:]
    column = "adj_close" if "adj_close" in frame.columns else "close"
    returns = frame[column].pct_change().abs()
    turnover = frame[column] * frame["volume"]
    ratio = (returns / turnover.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()
    return float(ratio.mean() * 1e9) if len(ratio) >= 30 else None


@metric("zero_return_days", pillar="liquidity", direction=-1, unit="fraction",
        needs_prices=True, min_history=126)
def zero_return_days(s: SecuritySnapshot) -> float | None:
    """Fraction of days with no price change — a classic proxy for thin trading."""
    returns = _log_returns(s, TRADING_DAYS)
    return float((returns.abs() < 1e-10).mean()) if returns is not None else None


# ---------------------------------------------------------------------------------------------
# Technical
# ---------------------------------------------------------------------------------------------


@metric("price_to_sma200", pillar="momentum", direction=1, unit="ratio",
        needs_prices=True, min_history=252, winsor=(0.0, 3.0))
def price_to_sma200(s: SecuritySnapshot) -> float | None:
    """Price relative to its 200-day simple moving average."""
    prices = _prices(s)
    if prices is None or len(prices) < 200:
        return None
    sma = float(prices.iloc[-200:].mean())
    return safe_div(float(prices.iloc[-1]), sma, positive_denominator=True)


@metric("golden_cross", pillar="momentum", direction=1, unit="binary",
        needs_prices=True, min_history=252)
def golden_cross(s: SecuritySnapshot) -> float | None:
    """1 when the 50-day average is above the 200-day average, else 0."""
    prices = _prices(s)
    if prices is None or len(prices) < 200:
        return None
    return float(prices.iloc[-50:].mean() > prices.iloc[-200:].mean())


@metric("rsi_14", pillar="momentum", direction=0, unit="index", ideal_band=(40.0, 65.0),
        needs_prices=True, min_history=63)
def rsi_14(s: SecuritySnapshot) -> float | None:
    """14-day RSI — **non-monotonic**, since both overbought and oversold are notable.

    Scored through a band rather than ranked: an RSI of 85 is not "better momentum", it is a
    stretched one, and an RSI of 15 is not simply "bad".
    """
    prices = _prices(s)
    if prices is None or len(prices) < 40:
        return None
    delta = prices.diff().dropna().iloc[-60:]
    if len(delta) < 20:
        return None
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    if loss <= 0:
        return 100.0
    rs = float(gain / loss)
    return float(100.0 - 100.0 / (1.0 + rs))


@metric("atr_percent", pillar="risk", direction=-1, unit="ratio",
        needs_prices=True, min_history=63)
def atr_percent(s: SecuritySnapshot) -> float | None:
    """Average true range as a percentage of price — an intraday-aware volatility measure."""
    if not s.has_prices:
        return None
    frame = s.prices.iloc[-40:]
    if not {"high", "low", "close"} <= set(frame.columns) or len(frame) < 20:
        return None
    high, low, close = frame["high"], frame["low"], frame["close"]
    previous = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1).dropna()
    if len(true_range) < 10 or close.iloc[-1] <= 0:
        return None
    return float(true_range.mean() / close.iloc[-1])
