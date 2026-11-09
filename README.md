# Long-Term Trader (paper-only)

An explainable, long-horizon research and paper-trading foundation for Indian and US equities. It **does not connect to brokers, submit live orders, use leverage, short stocks, or trade options**. Returns are uncertain; backtests are research, not evidence of future profitability.

## What Phase 1 includes

- CSV-first historical data ingestion, validated OHLC data, exchange-calendar session checks, split factors, payment-dated dividend events, and an optional `yfinance` research adapter.
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

Use a **survivorship-aware** source where possible (for example NSE/BSE licensed data, CRSP/Compustat, Refinitiv, FactSet, or a provider with point-in-time constituents/fundamentals). The built-in CSV reader expects one file per symbol in `data/raw/`:

```csv
date,open,high,low,close,adj_close,volume,dividend,split_factor,dividend_payment_date
2023-01-02,100,101,99,100,100,100000,0,1,
2023-03-15,105,106,104,105,105,80000,1.2,1,2023-04-10
```

`close` is the native-currency tradable close; `adj_close` must be vendor-adjusted for splits and dividends and is used only for indicators. `split_factor` is new shares per old share on that date (normally `1`). Dividend rows should include the actual `dividend_payment_date`; without it, legacy files are paid on the ex-date and overstate cash availability. Add fundamentals in `data/fundamentals.csv` with `symbol,as_of_date,dividend_yield,payout_ratio,fcf_positive,debt_to_equity,revenue_growth,earnings_growth,dividend_growth_5y,sector,currency,exchange`. `as_of_date` must be when the information was publicly available—not its fiscal period end—to prevent look-ahead bias. Multiple snapshots per symbol are supported; only the latest snapshot dated on or before each decision is used.

Sessions are validated through `pandas-market-calendars` using `exchange` (`NSE`, `BSE`, or `NYSE`); if omitted, INR defaults to NSE and USD to NYSE. Vendor rows on exchange holidays and missing quotes inside an instrument's active history are rejected.

For historical INR performance of US securities, provide `data/usdinr.csv` with `date,usd_inr` and pass `--fx-file data/usdinr.csv`. Otherwise the configured static rate is used, which is unsuitable for research results.

Run a transparent backtest (only after supplying real files):

```powershell
python -m trader.cli backtest --data-dir data/raw --fundamentals data/fundamentals.csv --start 2020-01-01 --end 2024-12-31 --fx-file data/usdinr.csv
```

This prints metrics calculated from the supplied data; it ships with **no invented market data or results**. To compare benchmarks, ingest `^NSEI.csv` and `^GSPC.csv` in the same format and pass `--benchmarks ^NSEI ^GSPC`.

Optional prototype download (check Yahoo terms, exchange coverage, corporate-action accuracy, rate limits, and licensing before use):

```powershell
python -m trader.cli download --symbols RELIANCE.NS TCS.NS AAPL MSFT --start 2020-01-01 --end 2025-01-01
```

`yfinance` is not a production data contract. It may have gaps, changed symbols, adjusted-price conventions, and rate limits. Production deployment requires a licensed provider and point-in-time corporate actions, FX, fundamentals, and constituent histories.

## Paper operation

The `paper-run` command is stateful. It restores SQLite portfolio state, processes only dates later than its last completed session, executes previously pending orders on the next available session open for that symbol, and records new pending orders after the latest close. Re-running unchanged data is idempotent and produces no duplicate trades. Schedule it once after data for the relevant exchanges is complete.

```powershell
python -m trader.cli paper-run --data-dir data/raw --fundamentals data/fundamentals.csv
uvicorn trader.api:app --reload
streamlit run src/trader/dashboard.py
```

The initial target sleeves are 30% dividend quality, 20% dividend growth, 30% growth, and 20% cash. Each sleeve is independently ranked, then capped by the configured per-position limit; overlapping signals cannot exceed that cap and any unusable sleeve budget remains cash. Purchases are capped by per-position and sector limits. The paper engine creates a controlled next-session reduction order when a position or sector breaches a limit by more than the rebalance threshold. Pending dividend receivables are included in portfolio equity; foreign-currency receivables are revalued using the supplied daily FX rate and converted to INR when paid. An empty signal set does not liquidate the paper portfolio.

## Automated tests\n\nGitHub Actions runs `python -m pytest -q` on pushes and pull requests to `main`. The regression suite covers zero/partial sells, dividend entitlement and payment accounting, FX revaluation, persistence, exchange-session execution, and point-in-time fundamentals. A green test suite is necessary but not sufficient evidence of strategy profitability or production readiness.\n\n## Architecture

`data` reads validated historical files; `strategies` produces explainable scores and sleeve targets; `backtest` uses completed-session signals and next-session open execution; `portfolio` models split-aware paper accounting; `paper` drives stateful forward simulation; `storage` persists the audit trail; `api`/`dashboard` display stored results. The provided universe is whatever files you supply—this does **not** eliminate survivorship bias. Supply historical membership, delistings, and point-in-time fundamentals to address it.

## Execution assumptions\n\nOrders are modelled at the next session open with configured basis-point costs and slippage. By default, an order is eligible only when its full share quantity is no more than 1% of that session's reported volume (`execution.max_volume_participation`); otherwise it remains pending for a later session. This is a conservative all-or-none liquidity gate, not a true order-book simulator: it does not model bid/ask depth, intraday volume curves, partial fills, price impact, limit-up/limit-down queues, or broker-specific fee schedules. The configured India/US cost basis points are estimates and must be calibrated against the intended broker and current exchange/tax schedules before interpreting results.\n\n## Tax / accounting caveat

Tax settings are deliberately simplified configurable estimates, not tax advice. Indian STCG/LTCG treatment, US capital-gains treatment, Indian dividend taxation, treaty claims, ADR handling, fees, FX spreads, and reporting are not fully modelled and depend on residency, holding period, broker, and security. Obtain professional advice before any real-money use.
