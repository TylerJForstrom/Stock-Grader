# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Bank-native metrics (efficiency ratio, net interest income to assets, fee income share,
  deposits/assets, loans/deposits, allowance coverage, provision burden, tangible common equity)
  and REIT FFO reconstruction with `price_to_ffo`. Banks previously got 36 of 65 price-free
  metrics with the efficiency pillar entirely empty.
- Daily adjusted OHLCV via `--stockanalysis`, bringing coverage to 100% and enabling all 40
  risk, momentum and liquidity metrics. Opt-in; see the module docstring for the caveat.
- Share prices derived from SEC filings — insider transaction prices (~4,000 tickers) with
  public-float fallback calibrated for affiliate holdings.
- FRED benchmark provider, so `beta`, `capm_alpha` and `idiosyncratic_volatility` can fire at all.
- `scripts/validate_distress.py` — measures AUC against real SEC outcome labels (going-concern
  opinions, bankruptcies, restatements) without needing any price data.
- `scripts/calibrate_intervals.py` — measures whether the reported 90% interval covers 90%.
- `fetch_by_cik` for historical work; the ticker map is survivor-filtered and reuses tickers.
- Letter-grade probability distribution alongside the point grade.
- Effective pillar weights and lost weight, distinct from the profile's nominal weights.
- `py.typed`, CI across Python 3.11-3.13, ruff and mypy configuration.

### Fixed
Each of these produced a confidently wrong number rather than an error.
- Stale tag selection: Lowe's read as 92% debt-free from a tag abandoned in 2009
  (debt/assets 0.08 against a true 0.72). 85 substitutions across 40 companies.
- The annual frame was the quarterly frame, so five-year CAGRs were computed over 1.25 years.
- Foreign currencies were read as USD — Toyota's JPY revenue came out ~150x too large.
- Bank revenue resolved to three incompatible bases at once (fee income, gross, net of interest).
- Hurst estimator bias: a true random walk returned 0.5996, putting 52% of ordinary stocks
  outside the metric's own random-walk band.
- Cornish-Fisher VaR inverted above its valid domain, scoring the fattest-tailed stock as safest.
- Altman Z'' rated Bed Bath & Beyond "safe" ten months before Chapter 11.
- Beneish's largest term and Piotroski's rescaling both fabricated components.
- A missing risk-free rate silently became 0%, a vol-dependent and therefore rank-changing bias.
- The reported "90% confidence interval" covered 0.70 falling to 0.40; now 1.00 to 0.86.
- Stock splits were not handled, so share series jumped 9.9x (NVDA), 20.2x (AMZN), 3.8x (AAPL).
- McDonald's tags diluted shares in millions; nothing cross-checked the scale.
- Derived quarters were ungated, letting a restatement-vintage mismatch produce negative capex.
- Market cap used the adjusted close, deflating every historical multiple by dividend yield.
- `--asof` was accepted and ignored under the default point-in-time mode.
- `momentum_1m` scored higher-is-better while `momentum_12_1` skips that month because it reverses.
- `accruals_ratio` scored AUC 0.29 against going-concern companies — it rewarded distress.
- Metrics computed but weighted at zero: risk, momentum and liquidity in every profile.

## [0.1.0]

Initial implementation: 105 metrics, 23 weighting methods, 10 normalizers, 8 aggregators,
11 profiles, SEC EDGAR XBRL data layer.
