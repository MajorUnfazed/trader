# Long-Term Trader (paper-only)

An explainable, long-horizon research and paper-trading foundation for Indian and US equities. It **does not connect to brokers, submit live orders, use leverage, short stocks, or trade options**. Returns are uncertain; backtests are research, not evidence of future profitability.

## What Phase 1 includes

- CSV-first historical data ingestion, duplicate/invalid market-data rejection, exchange-calendar session checks, explicit halt flags, split factors, payment-dated dividend events, and an optional `yfinance` research adapter.
- Three rules-based strategies: dividend quality, dividend growth, and long-term growth/momentum.
- Event-driven daily backtest with next-session execution, split-aware holdings, last-known-price marks between exchange sessions, configurable costs/slippage, a conservative all-or-none daily-volume participation gate, cash reserve, position caps, INR accounting, historical USD/INR support, dividend withholding, and simplified taxable realised gains.
- SQLite persistence for paper portfolios, positions, pending orders, fills, currency-denominated dividend receivables, decisions, processed sessions, and daily snapshots.
- FastAPI read-only status API and Streamlit dashboard.
- NIFTY 50 / S&P 500 benchmark comparison when their historical files are supplied.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,web,research]"
Copy-Item .env.example .env
Copy-Item config.example.yml config.yml
pytest -q
```

## Historical data and sample backtest

Read [the data-source decision record](docs/data-sources.md) before choosing a
vendor. It compares official Indian exchange sources, SEC/CRSP, Tiingo, EODHD,
benchmark and FX sources, and makes the licensing/coverage boundaries explicit.
The repository now includes CSV normalization, source sidecars, and opt-in
Tiingo/EODHD adapters; it includes neither credentials nor paid market data.

Run a structural data audit before backtesting:

```powershell
python -m trader.cli audit-data --data-dir data/raw --fundamentals data/fundamentals.csv --universe-membership data/universe_membership.csv --fx-file data/usdinr.csv
```

The audit reports symbol coverage, halts, corporate actions, delisting settlements, fundamentals coverage, membership, FX coverage, and known structural warnings. It cannot verify the truth, licensing, or completeness of a vendor's source data.

Use a **survivorship-aware** source where possible (for example NSE/BSE licensed data, CRSP/Compustat, Refinitiv, FactSet, or a provider with point-in-time constituents/fundamentals). The built-in CSV reader expects one file per symbol in `data/raw/`:

```csv
date,open,high,low,close,adj_close,volume,dividend,split_factor,dividend_payment_date,delisting_price
2023-01-02,100,101,99,100,100,100000,0,1,,
2023-03-15,105,106,104,105,105,80000,1.2,1,2023-04-10,
```

`close` is the native-currency tradable close; `adj_close` must be vendor-adjusted for splits and dividends and is used only for indicators. `split_factor` is new shares per old share on that date (normally `1`). The optional `delisting_price` column records explicit cash settlement per share on the final event row; the backtest cash-settles an open holding at that value and cancels pending orders. It must be sourced from an exchange/company settlement notice or a reliable vendor—do not fabricate it. Without a verified settlement value, a delisted holding cannot be valued reliably and should be flagged for manual resolution. Dividend rows should include the actual `dividend_payment_date`; without it, legacy files are paid on the ex-date and overstate cash availability. Add fundamentals in `data/fundamentals.csv` with `symbol,as_of_date,dividend_yield,payout_ratio,fcf_positive,debt_to_equity,revenue_growth,earnings_growth,dividend_growth_5y,sector,currency,exchange`. `as_of_date` records the financial period/snapshot date. Prefer the optional `available_at` timestamp for the actual public-release time; when present, it controls point-in-time selection, and the latest snapshot available by the end of each decision date is used. If `available_at` is absent, `as_of_date` is used as the availability date. Never substitute a fiscal period end for a publication date.

Instrument-specific halts should be represented by an exchange-session row with `tradable=false` and `volume=0`; carry the last known OHLC/adjusted-close values in that row. Halted rows remain in the calendar history but are excluded from marks, momentum indicators, and executions. Missing rows inside an instrument's active history are still rejected, so a halt must not be silently represented as a missing quote. The optional `tradable` CSV field accepts `true/false`, `1/0`, or `yes/no`.

Price CSVs reject duplicate dates, invalid/non-numeric OHLCV values, negative volume/dividends, and invalid split factors. Fundamentals reject duplicate symbol/date snapshots rather than selecting one arbitrarily.

Sessions are validated through `pandas-market-calendars` using `exchange` (`NSE`, `BSE`, or `NYSE`); if omitted, INR defaults to NSE and USD to NYSE. Vendor rows on exchange holidays and missing quotes inside an instrument's active history are rejected.

For a point-in-time eligible universe, provide `data/universe_membership.csv` with `symbol,start_date,end_date`. Dates are inclusive; leave `end_date` blank for current membership. Multiple non-overlapping intervals per symbol are supported. The backtest only generates signals for constituents active on each decision date, cancels stale buy intents after removal, and schedules an exit for held names that leave the supplied universe. Include historical constituents that later disappeared, plus their full price/corporate-action history, or survivorship bias remains. Delisted securities still require a reliable final trading/settlement price; this file is not a substitute for delisting-return data. If omitted, backtest metrics explicitly warn that survivorship bias may remain.

For historical INR performance of US securities, provide `data/usdinr.csv` with `date,usd_inr` and pass `--fx-file data/usdinr.csv`. Otherwise the configured static rate is used, which is unsuitable for research results.

Run a transparent backtest (only after supplying real files):

```powershell
python -m trader.cli backtest --data-dir data/raw --fundamentals data/fundamentals.csv --universe-membership data/universe_membership.csv --start 2020-01-01 --end 2024-12-31 --fx-file data/usdinr.csv
```

This prints metrics calculated from the supplied data; it ships with **no invented market data or results**. To compare benchmarks, ingest `^NSEI.csv` and `^GSPC.csv` in the same format and pass `--benchmarks ^NSEI ^GSPC`. Benchmark reports prefer a provider-supplied `total_return_index`; otherwise they calculate a transparent split/dividend-reinvestment proxy from `close`, `dividend`, and `split_factor`. The proxy assumes reinvestment at ex-date close and does not model dividend tax or withholding. `adj_close` is only a fallback proxy and is labelled as such. Add a `currency` column to benchmark CSVs when they are not INR; USD benchmarks are converted to INR only when the daily `--fx-file` is supplied, otherwise they are explicitly labelled unconverted.

Optional prototype download (check Yahoo terms, exchange coverage, corporate-action accuracy, rate limits, and licensing before use):

```powershell
python -m trader.cli download --symbols RELIANCE.NS TCS.NS AAPL MSFT --start 2020-01-01 --end 2025-01-01
```

`yfinance` is not a production data contract. It may have gaps, changed symbols, adjusted-price conventions, and rate limits. Production deployment requires a licensed provider and point-in-time corporate actions, FX, fundamentals, and constituent histories.

## Walk-forward research

Run sequential out-of-sample windows after a historical warm-up period:

```powershell
python -m trader.cli walk-forward --data-dir data/raw --fundamentals data/fundamentals.csv --universe-membership data/universe_membership.csv --start 2015-01-01 --end 2024-12-31 --train-days 1095 --test-days 365 --step-days 365 --fx-file data/usdinr.csv
```

Window lengths are calendar days. The current strategies are fixed rules, so the training window is used for historical warm-up/research, not parameter fitting. Every test fold is reported; compare the full sequence with matched total-return benchmarks rather than choosing the best fold. The report includes mean and median CAGR, return, drawdown, Sharpe, Sortino, and volatility. Add `--strategy dividend_quality`, `--strategy dividend_growth`, or `--strategy long_term_growth` to evaluate one sleeve at a time; isolated runs give that strategy the full investable allocation (cash reserve unchanged), so the results are comparable on capital budget.

## Paper operation

The `paper-run` command is stateful. It restores SQLite portfolio state, processes only dates later than its last completed session, executes previously pending orders on the next available session open for that symbol, and records new pending orders after the latest close. Re-running unchanged data is idempotent and produces no duplicate trades. Schedule it once after data for the relevant exchanges is complete.

```powershell
python -m trader.cli paper-run --data-dir data/raw --fundamentals data/fundamentals.csv
uvicorn trader.api:app --reload
streamlit run src/trader/dashboard.py
```

The initial target sleeves are 30% dividend quality, 20% dividend growth, 30% growth, and 20% cash. Each sleeve is independently ranked, then capped by the configured per-position limit; overlapping signals cannot exceed that cap and any unusable sleeve budget remains cash. Purchases are capped by per-position and sector limits. The paper engine creates a controlled next-session reduction order when a position or sector breaches a limit by more than the rebalance threshold. Pending dividend receivables are included in portfolio equity; foreign-currency receivables are revalued using the supplied daily FX rate and converted to INR when paid. An empty signal set does not liquidate the paper portfolio.

## Automated tests

GitHub Actions runs `python -m pytest -q` on pushes and pull requests to `main`. The regression suite covers zero/partial sells, dividend entitlement and payment accounting, FX revaluation, persistence, exchange-session execution, market-halt handling, point-in-time fundamentals, historical constituent membership, delisting settlement, total-return calculations, and walk-forward fold boundaries. A green test suite is necessary but not sufficient evidence of strategy profitability or production readiness.

## Architecture

`data` reads validated historical files; `strategies` produces explainable scores and sleeve targets; `backtest` uses completed-session signals and next-session open execution; `portfolio` models split-aware paper accounting; `paper` drives stateful forward simulation; `storage` persists the audit trail; `api`/`dashboard` display stored results. The provided universe is whatever files you supply—this does **not** eliminate survivorship bias. Supply historical membership, delistings, and point-in-time fundamentals to address it.

## Execution assumptions

Orders are modelled at the next session open with configured basis-point costs and slippage. By default, an order is eligible only when its full share quantity is no more than 1% of that session's reported volume (`execution.max_volume_participation`); otherwise it remains pending for a later session. This is a conservative all-or-none liquidity gate, not a true order-book simulator: it does not model bid/ask depth, intraday volume curves, partial fills, price impact, limit-up/limit-down queues, or broker-specific fee schedules. The configured India/US cost basis points are estimates and must be calibrated against the intended broker and current exchange/tax schedules before interpreting results.

## Tax / accounting caveat

Tax settings are deliberately simplified configurable estimates, not tax advice. Indian STCG/LTCG treatment, US capital-gains treatment, Indian dividend taxation, treaty claims, ADR handling, fees, FX spreads, and reporting are not fully modelled and depend on residency, holding period, broker, and security. Obtain professional advice before any real-money use.
