import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
import pandas as pd

from .config import load_config
from .data import load_directory, download_yfinance, read_fx_csv, read_universe_membership_csv, read_fundamentals_csv
from .providers import Provenance, import_price_csv, fetch_prices_with_fallback, write_dataset
from .backtest import run_backtest, benchmark_metrics
from .research import walk_forward_backtest
from .storage import connect, persist_result
from .paper import run_paper
from .audit import audit_data


def fundamentals(path):
    return read_fundamentals_csv(path)


def main():
    parser = argparse.ArgumentParser(description="Paper-only long-term trader")
    sub = parser.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download")
    dl.add_argument("--symbols", nargs="+", required=True)
    dl.add_argument("--start", required=True)
    dl.add_argument("--end", required=True)
    dl.add_argument("--output-dir", default="data/raw")

    imp = sub.add_parser("import-prices")
    imp.add_argument("--input", required=True)
    imp.add_argument("--output", required=True)
    imp.add_argument("--provider", required=True)
    imp.add_argument("--symbol")
    imp.add_argument("--source-url")
    imp.add_argument("--license-note")
    imp.add_argument("--adjustment-basis", default="provider_supplied")

    fetch = sub.add_parser("fetch-prices")
    fetch.add_argument("--provider", choices=("tiingo", "eodhd"), help="One remote source (legacy form).")
    fetch.add_argument("--providers", choices=("tiingo", "eodhd"), nargs="+", help="Ordered sources; advances only after HTTP 429.")
    fetch.add_argument("--symbol", required=True)
    fetch.add_argument("--provider-symbol", action="append", default=[], metavar="PROVIDER=SYMBOL",
                       help="Override a provider's symbol, repeatable (e.g. eodhd=AAPL.US).")
    fetch.add_argument("--start", required=True)
    fetch.add_argument("--end", required=True)
    fetch.add_argument("--output", required=True)

    for cmd in ("backtest", "paper-run", "walk-forward", "audit-data"):
        q = sub.add_parser(cmd)
        q.add_argument("--data-dir", default="data/raw")
        q.add_argument("--fundamentals", required=True)
        q.add_argument("--start")
        q.add_argument("--end")
        q.add_argument("--config", default="config.yml")
        q.add_argument("--database", default="data/trader.db")
        q.add_argument("--benchmarks", nargs="*")
        q.add_argument("--fx-file")
        if cmd in ("backtest", "walk-forward", "audit-data"):
            q.add_argument("--universe-membership")
        if cmd in ("backtest", "walk-forward"):
            q.add_argument("--strategy", choices=("all", "dividend_quality", "dividend_growth", "long_term_growth"), default="all")
        if cmd == "walk-forward":
            q.add_argument("--train-days", type=int, default=1095)
            q.add_argument("--test-days", type=int, default=365)
            q.add_argument("--step-days", type=int)

    a = parser.parse_args()
    if a.command == "download":
        download_yfinance(a.symbols, a.start, a.end, a.output_dir)
        return
    if a.command == "import-prices":
        import_price_csv(a.input, a.output, Provenance.now(a.provider, source_url=a.source_url,
                         license_note=a.license_note, adjustment_basis=a.adjustment_basis), symbol=a.symbol)
        return
    if a.command == "fetch-prices":
        if bool(a.provider) == bool(a.providers):
            parser.error("fetch-prices requires exactly one of --provider or --providers")
        selected = [a.provider] if a.provider else a.providers
        symbols = {"default": a.symbol}
        for entry in a.provider_symbol:
            if "=" not in entry:
                parser.error("--provider-symbol must be PROVIDER=SYMBOL")
            provider_name, provider_symbol = entry.split("=", 1)
            if provider_name not in selected or not provider_symbol:
                parser.error("--provider-symbol must name a selected provider and a non-empty symbol")
            symbols[provider_name] = provider_symbol
        try:
            frame, used_provider = fetch_prices_with_fallback(selected, symbols, a.start, a.end)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        except HTTPError as exc:
            parser.error(f"remote provider request failed: HTTP {exc.code} {exc.reason}")
        write_dataset(a.output, frame, Provenance.now(used_provider, source_url=f"provider API: {a.symbol}",
                      license_note="Confirm subscription, exchange entitlements, and redistribution rights before use."))
        return

    prices = load_directory(a.data_dir)
    if a.command == "audit-data":
        membership = read_universe_membership_csv(a.universe_membership) if a.universe_membership else None
        fx = read_fx_csv(a.fx_file) if a.fx_file else None
        print(json.dumps(audit_data(prices, fundamentals(a.fundamentals), membership, fx), indent=2))
        return
    all_dates = [d for frame in prices.values() for d in frame.index]
    benchmark_data = {symbol: prices.pop(symbol) for symbol in (a.benchmarks or []) if symbol in prices}
    if not prices:
        raise ValueError("No tradable price files remain after excluding benchmarks")
    cfg = load_config(a.config)
    fx = read_fx_csv(a.fx_file) if a.fx_file else None
    f = fundamentals(a.fundamentals)

    if a.command == "paper-run":
        report = run_paper(prices, f, cfg, connect(a.database), a.end, fx)
        print(json.dumps({"processed_sessions": report["processed"], "cash_inr": report["portfolio"].cash_inr, "positions": len(report["portfolio"].positions)}, indent=2))
        return

    membership = read_universe_membership_csv(a.universe_membership) if a.universe_membership else None
    first = a.start or str(min(all_dates).date())
    last = a.end or str(max(all_dates).date())

    if a.command == "walk-forward":
        report = walk_forward_backtest(
            prices, f, cfg, first, last,
            train_days=a.train_days, test_days=a.test_days, step_days=a.step_days,
            usd_inr=fx, universe_membership=membership,
            strategy_filter=None if a.strategy == "all" else a.strategy,
        )
        for fold in report["folds"]:
            fold["benchmarks"] = {
                symbol: benchmark_metrics(
                    frame, fold["test_start"], fold["test_end"], usd_inr=fx,
                    currency=(str(frame["currency"].dropna().iloc[0]).upper()
                              if "currency" in frame and frame["currency"].notna().any()
                              else ("USD" if symbol.upper() in {"^GSPC", "SPY", "VOO"} else "INR")),
                )
                for symbol, frame in benchmark_data.items()
            }
        print(json.dumps(report, indent=2))
        return

    result = run_backtest(prices, f, cfg, first, last, usd_inr=fx, universe_membership=membership,
                          strategy_filter=None if a.strategy == "all" else a.strategy)
    persist_result(result, connect(a.database))
    report = result.metrics()
    report["benchmarks"] = {
        symbol: benchmark_metrics(
            frame, first, last, usd_inr=fx,
            currency=(str(frame["currency"].dropna().iloc[0]).upper()
                      if "currency" in frame and frame["currency"].notna().any()
                      else ("USD" if symbol.upper() in {"^GSPC", "SPY", "VOO"} else "INR")),
        )
        for symbol, frame in benchmark_data.items()
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
