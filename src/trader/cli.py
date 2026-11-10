import argparse
import json
from pathlib import Path
import pandas as pd

from .config import load_config
from .data import load_directory, download_yfinance, read_fx_csv, read_universe_membership_csv
from .backtest import run_backtest, benchmark_metrics
from .research import walk_forward_backtest
from .storage import connect, persist_result
from .paper import run_paper


def fundamentals(path):
    return pd.read_csv(path, parse_dates=["as_of_date"])


def main():
    parser = argparse.ArgumentParser(description="Paper-only long-term trader")
    sub = parser.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download")
    dl.add_argument("--symbols", nargs="+", required=True)
    dl.add_argument("--start", required=True)
    dl.add_argument("--end", required=True)
    dl.add_argument("--output-dir", default="data/raw")

    for cmd in ("backtest", "paper-run", "walk-forward"):
        q = sub.add_parser(cmd)
        q.add_argument("--data-dir", default="data/raw")
        q.add_argument("--fundamentals", required=True)
        q.add_argument("--start")
        q.add_argument("--end")
        q.add_argument("--config", default="config.yml")
        q.add_argument("--database", default="data/trader.db")
        q.add_argument("--benchmarks", nargs="*")
        q.add_argument("--fx-file")
        if cmd in ("backtest", "walk-forward"):
            q.add_argument("--universe-membership")
        if cmd == "walk-forward":
            q.add_argument("--train-days", type=int, default=756)
            q.add_argument("--test-days", type=int, default=252)
            q.add_argument("--step-days", type=int)

    a = parser.parse_args()
    if a.command == "download":
        download_yfinance(a.symbols, a.start, a.end, a.output_dir)
        return

    prices = load_directory(a.data_dir)
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
        )
        print(json.dumps(report, indent=2))
        return

    result = run_backtest(prices, f, cfg, first, last, usd_inr=fx, universe_membership=membership)
    persist_result(result, connect(a.database))
    report = result.metrics()
    report["benchmarks"] = {
        symbol: benchmark_metrics(frame, first, last)
        for symbol, frame in benchmark_data.items()
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
