import pandas as pd
import pytest

from trader.config import Config, ExecutionConfig
from trader.portfolio import Portfolio, Position
from trader.backtest import run_backtest


def test_volume_participation_gate_rejects_oversized_buy_and_sell():
    cfg = ExecutionConfig(max_volume_participation=0.01, india_cost_bps=0, slippage_bps=0)
    p = Portfolio(20_000)
    when = pd.Timestamp("2024-01-02").date()

    # 100 shares would be 10% of 1,000 shares traded: no assumed fill.
    assert p.buy("IN", 100, "INR", "Test", 10_000, cfg, when, "test", volume=1_000) is None
    assert p.cash_inr == 20_000

    # At 20,000 shares of daily volume, the same order is within the 1% cap.
    fill = p.buy("IN", 100, "INR", "Test", 10_000, cfg, when, "test", volume=20_000)
    assert fill is not None and fill.quantity == 100

    # A two-share sale cannot execute against a 100-share session at 1%.
    assert p.sell("IN", 100, cfg, when, "test", quantity=2, volume=100) is None
    assert p.positions["IN"].quantity == 100


def test_volume_participation_configuration_is_validated():
    with pytest.raises(ValueError, match="max_volume_participation"):
        Config(execution=ExecutionConfig(max_volume_participation=0))
    with pytest.raises(ValueError, match="max_volume_participation"):
        Config(execution=ExecutionConfig(max_volume_participation=1.1))


def test_backtest_keeps_order_pending_until_volume_supports_execution():
    days = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    price = pd.Series([100.0, 100.0, 100.0], index=days)
    prices = {
        "ABC": pd.DataFrame({
            "open": price, "high": price, "low": price, "close": price,
            "adj_close": price, "volume": [1_000, 1_000, 20_000],
            "dividend": 0.0,
        }, index=days)
    }
    fundamentals = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2024-01-01", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "dividend_growth_5y": 0.1, "sector": "Test", "currency": "INR",
    }])

    result = run_backtest(
        prices, fundamentals, Config(), str(days[0].date()), str(days[-1].date()),
        rebalance_days=1, validate_calendars=False,
    )
    fills = [fill for fill in result.portfolio.fills if fill.symbol == "ABC" and fill.side == "BUY"]
    assert len(fills) == 1
    assert fills[0].date == days[-1].date()
    assert fills[0].quantity == 100
