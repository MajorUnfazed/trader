import pandas as pd
import pytest

from trader.backtest import benchmark_metrics, run_backtest
from trader.data import total_return_index
from trader.research import walk_forward_backtest
from trader.config import Config


def test_total_return_index_reinvests_dividend_and_applies_split():
    frame = pd.DataFrame(
        {"close": [100.0, 50.0, 51.0], "dividend": [0.0, 1.0, 0.0],
         "split_factor": [1.0, 2.0, 1.0]},
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
    prices = {"A": pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "adj_close": close, "volume": 1000, "dividend": 0,
    }, index=dates)}
    with pytest.raises(ValueError, match="Insufficient history"):
        walk_forward_backtest(prices, pd.DataFrame(columns=["symbol", "as_of_date"]), Config(),
                              train_days=365, test_days=100, validate_calendars=False)


def test_backtest_cash_settles_explicit_delisting_price():
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    close = pd.Series([100.0, 100.0, 100.0], index=dates)
    prices = {"ABC": pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "adj_close": close, "volume": 100_000, "dividend": 0.0,
        "delisting_price": [float("nan"), float("nan"), 20.0],
    }, index=dates)}
    fundamentals = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2024-01-01", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "dividend_growth_5y": 0.0, "sector": "Test", "currency": "INR",
    }])
    result = run_backtest(prices, fundamentals, Config(), str(dates[0].date()),
                          str(dates[-1].date()), validate_calendars=False, rebalance_days=1)
    assert "ABC" not in result.portfolio.positions
    settlements = [fill for fill in result.portfolio.fills if fill.reason == "Cash delisting settlement"]
    assert len(settlements) == 1
    assert settlements[0].price_native == 20.0

from trader.portfolio import Position
from trader.storage import connect, save_portfolio
from trader.paper import run_paper


def test_paper_cash_settles_delisting_and_records_audit_fill():
    day = pd.Timestamp("2024-01-03")
    close = pd.Series([100.0], index=[day])
    prices = {"ABC": pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "adj_close": close, "volume": 0, "dividend": 0.0,
        "tradable": False, "delisting_price": [20.0],
    }, index=[day])}
    db = connect(":memory:")
    portfolio = __import__("trader.portfolio", fromlist=["Portfolio"]).Portfolio(0)
    portfolio.positions["ABC"] = Position("ABC", 10, 100.0, 100.0, "INR", "Test")
    save_portfolio(db, portfolio, "2024-01-02")
    db.commit()
    result = run_paper(prices, pd.DataFrame(columns=["symbol", "as_of_date"]), Config(),
                       db, as_of=day, validate_calendars=False)
    assert "ABC" not in result["portfolio"].positions
    row = db.execute("SELECT side,quantity,price_native,reason FROM fills").fetchone()
    assert row == ("SELL", 10, 20.0, "Cash delisting settlement")

from trader.strategies import fundamentals_as_of


def test_fundamentals_use_publication_timestamp_when_available():
    frame = pd.DataFrame([
        {"symbol": "A", "as_of_date": "2024-01-01", "available_at": "2024-01-05T15:30:00", "payout_ratio": 0.4},
        {"symbol": "A", "as_of_date": "2024-01-01", "available_at": "2024-02-05T15:30:00", "payout_ratio": 0.9},
    ])
    selected = fundamentals_as_of(frame, "2024-01-05")
    assert selected.loc["A", "payout_ratio"] == 0.4


def test_backtest_can_isolate_one_strategy_sleeve():
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    close = pd.Series([100.0, 100.0, 100.0], index=dates)
    prices = {"ABC": pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "adj_close": close, "volume": 100_000, "dividend": 0.0,
    }, index=dates)}
    fundamentals = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2024-01-01", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "dividend_growth_5y": 0.0, "sector": "Test", "currency": "INR",
    }])
    result = run_backtest(prices, fundamentals, Config(), str(dates[0].date()),
                          str(dates[-1].date()), validate_calendars=False,
                          strategy_filter="dividend_quality")
    assert result.metrics()["strategy"] == "dividend_quality"
    assert any(signal.strategy == "dividend_quality" for signal in result.decisions)
    assert all(signal.strategy == "dividend_quality" for signal in result.decisions)

def test_us_benchmark_is_converted_to_inr_with_daily_fx():
    dates = pd.date_range("2024-01-01", periods=3)
    frame = pd.DataFrame({
        "close": [100.0, 110.0, 110.0], "dividend": [0.0, 0.0, 0.0],
        "split_factor": [1.0, 1.0, 1.0],
    }, index=dates)
    fx = pd.Series([80.0, 90.0, 90.0], index=dates)
    report = benchmark_metrics(frame, "2024-01-01", "2024-01-03", usd_inr=fx, currency="USD")
    assert report["total_return"] == pytest.approx(0.2375)
    assert report["fx_adjusted_to_inr"] is True
    assert report["currency"] == "INR"

from trader.audit import audit_data


def test_data_audit_flags_missing_survivorship_and_fx_inputs():
    dates = pd.date_range("2024-01-01", periods=2)
    close = pd.Series([100.0, 101.0], index=dates)
    prices = {"US": pd.DataFrame({
        "close": close, "dividend": 0.0, "split_factor": 1.0,
        "tradable": True,
    }, index=dates)}
    fundamentals = pd.DataFrame([{
        "symbol": "US", "as_of_date": "2023-12-31", "currency": "USD",
    }])
    report = audit_data(prices, fundamentals)
    assert report["status"] == "WARN"
    assert any("survivorship" in warning for warning in report["warnings"])
    assert any("static FX" in warning for warning in report["warnings"])
    assert report["coverage"]["US"]["rows"] == 2
