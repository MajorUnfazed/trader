"""Rolling out-of-sample evaluation helpers for fixed, rules-based strategies."""
from __future__ import annotations

import pandas as pd

from .backtest import run_backtest
from .config import Config


def walk_forward_backtest(
    prices: dict[str, pd.DataFrame],
    fundamentals: pd.DataFrame,
    cfg: Config,
    start: str | None = None,
    end: str | None = None,
    train_days: int = 1095,
    test_days: int = 365,
    step_days: int | None = None,
    rebalance_days: int = 21,
    usd_inr: pd.Series | None = None,
    universe_membership: pd.DataFrame | None = None,
    validate_calendars: bool = True,
) -> dict:
    """Run sequential out-of-sample test windows using calendar-day windows.

    Strategies are fixed rules and have no fitted parameters. train_days is
    therefore a rolling research/warm-up window, not a model-fitting period.
    Historical prices before each test window remain available to indicators.
    """
    if train_days < 1 or test_days < 1 or (step_days is not None and step_days < 1):
        raise ValueError("train_days, test_days, and step_days must be positive")
    step = step_days or test_days
    all_dates = sorted(set().union(*(set(frame.index) for frame in prices.values())))
    if not all_dates:
        raise ValueError("No price history supplied")
    first = pd.Timestamp(start) if start else pd.Timestamp(all_dates[0])
    last = pd.Timestamp(end) if end else pd.Timestamp(all_dates[-1])
    if last <= first:
        raise ValueError("end must be after start")
    cursor = first + pd.Timedelta(days=train_days)
    folds = []
    while cursor <= last:
        fold_end = min(cursor + pd.Timedelta(days=test_days - 1), last)
        result = run_backtest(
            prices, fundamentals, cfg, str(cursor.date()), str(fold_end.date()),
            rebalance_days=rebalance_days, usd_inr=usd_inr,
            validate_calendars=validate_calendars,
            universe_membership=universe_membership,
        )
        folds.append({
            "train_start": str((cursor - pd.Timedelta(days=train_days)).date()),
            "train_end": str((cursor - pd.Timedelta(days=1)).date()),
            "test_start": str(cursor.date()),
            "test_end": str(fold_end.date()),
            "metrics": result.metrics(),
        })
        cursor += pd.Timedelta(days=step)
    if not folds:
        raise ValueError("Insufficient history for the requested training and test windows")
    names = ("cagr", "total_return", "max_drawdown", "sharpe", "sortino", "volatility")
    summary = {}
    for name in names:
        values = pd.Series([fold["metrics"][name] for fold in folds])
        summary[f"mean_{name}"] = float(values.mean())
        summary[f"median_{name}"] = float(values.median())
    summary["fold_count"] = len(folds)
    summary["method"] = "rolling_out_of_sample_fixed_rules"
    summary["warning"] = (
        "Strategies are fixed rules; the training window is not used to fit parameters. "
        "Inspect every fold, avoid selecting only the best period, and compare against matched total-return benchmarks."
    )
    return {"summary": summary, "folds": folds}
