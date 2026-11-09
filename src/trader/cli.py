import argparse, json
from pathlib import Path
import pandas as pd
from .config import load_config
from .data import load_directory, download_yfinance, read_fx_csv, read_universe_membership_csv
from .backtest import run_backtest, benchmark_metrics
from .storage import connect, persist_result
from .paper import run_paper

def fundamentals(path): return pd.read_csv(path, parse_dates=["as_of_date"])
def main():
    parser=argparse.ArgumentParser(description="Paper-only long-term trader")
    sub=parser.add_subparsers(dest="command",required=True)
    dl=sub.add_parser("download"); dl.add_argument("--symbols",nargs="+",required=True); dl.add_argument("--start",required=True); dl.add_argument("--end",required=True); dl.add_argument("--output-dir",default="data/raw")
    for cmd in ("backtest","paper-run"):
        q=sub.add_parser(cmd); q.add_argument("--data-dir",default="data/raw"); q.add_argument("--fundamentals",required=True); q.add_argument("--start"); q.add_argument("--end"); q.add_argument("--config",default="config.yml"); q.add_argument("--database",default="data/trader.db"); q.add_argument("--benchmarks",nargs="*"); q.add_argument("--fx-file")
        if cmd == "backtest": q.add_argument("--universe-membership")
    a=parser.parse_args()
    if a.command=="download": download_yfinance(a.symbols,a.start,a.end,a.output_dir); return
    prices=load_directory(a.data_dir); all_dates=[d for x in prices.values() for d in x.index]
    benchmark_data={s:prices.pop(s) for s in (a.benchmarks or []) if s in prices}
    if not prices: raise ValueError("No tradable price files remain after excluding benchmarks")
    cfg=load_config(a.config); fx=read_fx_csv(a.fx_file) if a.fx_file else None
    if a.command == "paper-run":
        report=run_paper(prices,fundamentals(a.fundamentals),cfg,connect(a.database),a.end,fx)
        print(json.dumps({"processed_sessions":report["processed"],"cash_inr":report["portfolio"].cash_inr,"positions":len(report["portfolio"].positions)},indent=2)); return
    membership = read_universe_membership_csv(a.universe_membership) if a.universe_membership else None
    result=run_backtest(prices,fundamentals(a.fundamentals),cfg,a.start or str(min(all_dates).date()),a.end or str(max(all_dates).date()),usd_inr=fx,universe_membership=membership)
    persist_result(result,connect(a.database)); report=result.metrics()
    report["benchmarks"]={s:benchmark_metrics(df,a.start or str(min(all_dates).date()),a.end or str(max(all_dates).date())) for s,df in benchmark_data.items()}
    print(json.dumps(report,indent=2))
if __name__=="__main__": main()
