import pandas as pd
import pytest

from trader.backtest import benchmark_metrics
from trader.data import total_return_index
from trader.research import walk_forward_backtest
from trader.config import Config


def test_total_return_index_reinvests_dividend_and_applies_split():
    frame = pd.DataFrame(
        {
            "close": [100.0, 50.0, 51.0],
            "dividend": [0.0, 1.0, 0.0],
            "split_factor": [1.0, 2.0, 1.0],
        },
        index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
    )
    index = total_return_index(frame)
    assert index.iloc[0] == 100.0
    assert index.iloc[1] == 102.0
    assert index.iloc[2] == pytest.approx(104.04)


def test_benchmark_metrics_uses_total_return_proxy_not_price_only():
    frame = pd.DataFrame(
        {"close": [100.0, 100.0, 100.0], "dividend": [0.0, 5.0, 0.0],
         "split_factor": [1.0, 1.0, 1.0], "adj_close": [100.0, 100.0, 100.0]},
        index=pd.date_range("2024-01-01", periods=3),
    )
    report = benchmark_metrics(frame, "2024-01-01", "2024-01-03")
    assert report["total_return"] == pytest.approx(0.05)
    assert report["return_basis"] == "reinvested_dividend_split_proxy"


def test_walk_forward_creates_sequential_out_of_sample_folds():
    dates = pd.bdate_range("2020-01-01", "2023-12-31")
    close = pd.Series(range(100, 100 + len(dates)), index=dates, dtype=float)
    prices = {"ABC": pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "adj_close": close, "volume": 100_000, "dividend": 0.0,
        "split_factor": 1.0,
    }, index=dates)}
    fundamentals = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2019-12-31", "dividend_yield": 0.0,
        "payout_ratio": 0.5, "fcf_positive": False, "debt_to_equity": 0.2,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.0,
        "sector": "Test", "currency": "INR",
    }])
    report = walk_forward_backtest(
        prices, fundamentals, Config(), start="2020-01-01", end="2023-12-31",
        train_days=365, test_days=180, step_days=180, validate_calendars=False,
    )
    assert report["summary"]["fold_count"] >= 3
    folds = report["folds"]
    assert all(pd.Timestamp(folds[i]["test_start"]) > pd.Timestamp(folds[i-1]["test_end"]) for i in range(1, len(folds)))
    assert "fixed rules" in report["summary"]["warning"]


def test_walk_forward_rejects_insufficient_history():
    dates = pd.bdate_range("2023-01-01", periods=10)
    close = pd.Series(100.0, index=dates)
    prices = {"A": pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "adj_close": close, "volume": 1000, "dividend": 0}, index=dates)}
    with pytest.raises(ValueError, match="Insufficient history"):
        walk_forward_backtest(prices, pd.DataFrame(columns=["symbol", "as_of_date"]), Config(),
                              train_days=365, test_days=100, validate_calendars=False)
