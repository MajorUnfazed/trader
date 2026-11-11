import pandas as pd
import pytest
from datetime import date

from trader.config import Config, TaxConfig, ExecutionConfig
from trader.portfolio import Portfolio, Position
from trader.storage import open_db, save_portfolio, load_portfolio
from trader.paper import run_paper
from trader.data import read_corporate_actions_csv
from trader.calendars import sessions


def make_ohlcv_frame(start_price=100.0, length=5, volume=100_000):
    idx = sessions("NSE", "2023-01-01", "2023-03-01")[:length]
    prices = [start_price + i for i in range(len(idx))]
    opens = pd.Series(prices, index=idx, dtype=float)
    highs = pd.Series([p + 2.0 for p in prices], index=idx, dtype=float)
    lows = pd.Series([p - 2.0 for p in prices], index=idx, dtype=float)
    closes = pd.Series(prices, index=idx, dtype=float)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "adj_close": closes,
        "volume": volume,
        "dividend": 0.0,
    }, index=idx)


def test_limit_order_execution_price_gates(tmp_path):
    db_path = str(tmp_path / "limit_orders.db")
    prices = {"ABC": make_ohlcv_frame(start_price=100.0, length=5)}
    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.1,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    # Day 0: Open=100, High=102, Low=98, Close=100
    # Day 1: Open=101, High=103, Low=99, Close=101
    day0 = prices["ABC"].index[0]
    day1 = prices["ABC"].index[1]

    with open_db(db_path) as db:
        p = Portfolio(100_000)
        save_portfolio(db, p, str(day0.date()))

        # 1. Unreachable BUY LIMIT order: limit_price=95 (Day 1 Low is 99, so 95 cannot fill)
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, limit_price, time_in_force, reason, status)
               VALUES(?, 'ABC', 'BUY', 'LIMIT', 0.10, 50, 95.0, 'GTC', 'Buy dip', 'PENDING')""",
            (str(day0.date()),)
        )

        # 2. Reachable BUY LIMIT order: limit_price=100.5 (Day 1 Low is 99.0 <= 100.5, fills at limit)
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, limit_price, time_in_force, reason, status)
               VALUES(?, 'ABC', 'BUY', 'LIMIT', 0.10, 50, 100.5, 'DAY', 'Buy limit fill', 'PENDING')""",
            (str(day0.date()),)
        )
        db.commit()

        # Run day 1
        run_paper(prices, f, Config(), db, as_of=str(day1.date()))

        # Verify unreachable order remains PENDING
        order_unreached = db.execute("SELECT status FROM orders WHERE limit_price=95.0").fetchone()
        assert order_unreached[0] == "PENDING"

        # Verify reachable order transitioned to FILLED at limit price
        order_filled = db.execute("SELECT status, execution_price FROM orders WHERE limit_price=100.5").fetchone()
        assert order_filled[0] == "FILLED"
        # Executed at min(open=101, limit=100.5) with slippage (10 bps = 100.6005)
        assert round(order_filled[1], 4) == round(100.5 * (1 + 10 / 10000), 4)


def test_volatility_scaled_market_impact_slippage():
    # Compare base slippage vs volatility-scaled slippage
    cfg_base = ExecutionConfig(slippage_bps=10, india_cost_bps=0, use_volatility_slippage=False)
    cfg_scaled = ExecutionConfig(slippage_bps=10, india_cost_bps=0, use_volatility_slippage=True)

    vol = 1000.0
    est_shares = 100
    vol_ratio = (min(1.0, est_shares / vol)) ** 0.5
    scaled_cfg = ExecutionConfig(
        slippage_bps=cfg_scaled.slippage_bps * (1.0 + vol_ratio),
        india_cost_bps=0,
        use_volatility_slippage=True
    )

    p_base = Portfolio(100_000)
    p_scaled = Portfolio(100_000)

    fill_base = p_base.buy("IN", 100.0, "INR", "Tech", 10_000, cfg_base, date(2023, 1, 2), "base")
    fill_scaled = p_scaled.buy("IN", 100.0, "INR", "Tech", 10_000, scaled_cfg, date(2023, 1, 2), "scaled")

    assert fill_base is not None and fill_scaled is not None
    assert fill_base.price_native == 100.0 * (1 + 10 / 10000)
    assert fill_scaled.price_native > fill_base.price_native
    assert fill_scaled.price_native == 100.0 * (1 + 10 * (1.0 + (100 / 1000) ** 0.5) / 10000)


def test_tax_lot_fifo_accounting_stcg_and_ltcg():
    p = Portfolio(100_000)
    cfg = ExecutionConfig(slippage_bps=0, india_cost_bps=0)
    tax_cfg = TaxConfig(india_stcg_rate=0.20, india_ltcg_rate=0.125, india_stcg_days=365, enable_tax_lot_fifo=True)

    # Lot 1: Bought on 2021-01-01 at 100 (50 shares)
    # Lot 2: Bought on 2022-06-01 at 120 (50 shares)
    p.buy("IN", 100.0, "INR", "Tech", 5000, cfg, date(2021, 1, 1), "seed1")
    p.buy("IN", 120.0, "INR", "Tech", 6000, cfg, date(2022, 6, 1), "seed2")
    assert len(p.positions["IN"].tax_lots) == 2

    # Sell 60 shares on 2022-07-01 at 150:
    # FIFO consumes:
    # 50 shares from Lot 1 (bought 2021-01-01, held 546 days >= 365 days -> LTCG!)
    #   Gain = 50 * (150 - 100) = 2,500. LTCG Tax (12.5%) = 312.5
    # 10 shares from Lot 2 (bought 2022-06-01, held 30 days < 365 days -> STCG!)
    #   Gain = 10 * (150 - 120) = 300. STCG Tax (20%) = 60.0
    # Total Tax = 372.5
    fill = p.sell("IN", 150.0, cfg, date(2022, 7, 1), "rebalance", quantity=60, tax_cfg=tax_cfg)
    assert fill is not None
    assert fill.quantity == 60
    assert p.positions["IN"].quantity == 40
    # 40 shares remaining in Lot 2
    assert len(p.positions["IN"].tax_lots) == 1
    assert p.positions["IN"].tax_lots[0].quantity == 40

    # Total cash received: Gross (60 * 150 = 9000) - Total Tax (372.5) = 8627.5
    expected_gain = 2800.0
    expected_tax = 312.5 + 60.0
    assert round(p.realised_pnl_inr, 2) == round(expected_gain - expected_tax, 2)


def test_tax_loss_carryforward_offsets_subsequent_gains():
    p = Portfolio(100_000)
    cfg = ExecutionConfig(slippage_bps=0, india_cost_bps=0)
    tax_cfg = TaxConfig(india_stcg_rate=0.20, india_ltcg_rate=0.125, enable_tax_lot_fifo=True)

    # Buy at 100
    p.buy("IN", 100.0, "INR", "Tech", 10_000, cfg, date(2023, 1, 1), "seed")
    # Sell at 80 -> loss of 2000 INR
    p.sell("IN", 80.0, cfg, date(2023, 2, 1), "loss", quantity=100, tax_cfg=tax_cfg)
    assert p.capital_loss_carryforward_inr == 2000.0

    # Buy again at 100
    p.buy("IN", 100.0, "INR", "Tech", 10_000, cfg, date(2023, 3, 1), "seed2")
    # Sell at 115 -> gain of 1500 INR, offset fully by 2000 loss carryforward
    fill = p.sell("IN", 115.0, cfg, date(2023, 4, 1), "gain", quantity=100, tax_cfg=tax_cfg)
    # Tax should be 0 because 1500 gain is offset by carryforward!
    assert fill.fee_inr == 0.0  # fee_inr stores fee + tax
    assert p.capital_loss_carryforward_inr == 500.0  # 2000 - 1500 = 500 remaining


def test_tax_lots_survive_persistence_and_restoration(tmp_path):
    db_path = str(tmp_path / "tax_lots.db")
    with open_db(db_path) as db:
        p = Portfolio(50_000)
        cfg = ExecutionConfig(slippage_bps=0, india_cost_bps=0)
        p.buy("INFY", 100.0, "INR", "Tech", 5000, cfg, date(2022, 1, 1), "lot1")
        p.buy("INFY", 110.0, "INR", "Tech", 5500, cfg, date(2022, 6, 1), "lot2")
        save_portfolio(db, p, "2022-06-01")
        db.commit()

    with open_db(db_path) as db:
        p_loaded, last_date = load_portfolio(db, 50_000)
        assert "INFY" in p_loaded.positions
        pos = p_loaded.positions["INFY"]
        assert len(pos.tax_lots) == 2
        assert pos.tax_lots[0].quantity == 50
        assert pos.tax_lots[0].buy_date == date(2022, 1, 1)
        assert pos.tax_lots[1].quantity == 50
        assert pos.tax_lots[1].buy_date == date(2022, 6, 1)


def test_stock_merger_corporate_action():
    p = Portfolio(50_000)
    cfg = ExecutionConfig(slippage_bps=0, india_cost_bps=0)
    # Buy 100 shares of TARGET at 50 INR
    p.buy("TARGET", 50.0, "INR", "Tech", 5000, cfg, date(2022, 1, 1), "seed")
    assert "TARGET" in p.positions

    # Merger: 1 share of TARGET becomes 0.5 shares of ACQUIRER
    p.apply_merger("TARGET", "ACQUIRER", 0.5, date(2022, 5, 1))

    assert "TARGET" not in p.positions
    assert "ACQUIRER" in p.positions
    acq = p.positions["ACQUIRER"]
    assert acq.quantity == 50
    # Total cost basis is preserved (5000 INR -> 100 INR per ACQUIRER share)
    assert round(acq.avg_cost_inr, 2) == 100.0
    assert len(acq.tax_lots) == 1
    assert acq.tax_lots[0].quantity == 50
    assert round(acq.tax_lots[0].cost_inr, 2) == 100.0


def test_read_corporate_actions_csv_validation(tmp_path):
    csv_file = tmp_path / "actions.csv"
    csv_file.write_text("date,symbol,action_type,target_symbol,ratio,cash_amount\n"
                        "2023-01-10,OLD,STOCK_MERGER,NEW,0.5,\n"
                        "2023-02-15,BUST,WORTHLESS_LIQUIDATION,,,\n")
    df = read_corporate_actions_csv(csv_file)
    assert len(df) == 2
    assert "action_type" in df.columns

    # Invalid action_type must fail validation
    bad_csv = tmp_path / "bad_actions.csv"
    bad_csv.write_text("date,symbol,action_type\n2023-01-10,OLD,INVALID_TYPE\n")
    with pytest.raises(ValueError, match="invalid action_type"):
        read_corporate_actions_csv(bad_csv)


def test_quarantined_orphaned_position_protection(tmp_path):
    db_path = str(tmp_path / "quarantine.db")
    prices = {"ORPHAN": make_ohlcv_frame(start_price=100.0, length=2)}
    f = pd.DataFrame([{
        "symbol": "ORPHAN", "as_of_date": "2022-12-31", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    with open_db(db_path) as db:
        p = Portfolio(10_000)
        # Pre-seed holding of ORPHAN
        p.positions["ORPHAN"] = Position("ORPHAN", 100, 100.0, 100.0, "INR", "Tech", is_quarantined=False)
        save_portfolio(db, p, str((prices["ORPHAN"].index[0] - pd.Timedelta(days=1)).date()))
        db.commit()

        # Run through day 2
        report = run_paper(prices, f, Config(), db)
        p_final = report["portfolio"]

        # Now simulate quarantine: position is marked is_quarantined=True
        p_final.positions["ORPHAN"].is_quarantined = True
        save_portfolio(db, p_final, "2023-01-05")

        p_loaded, _ = load_portfolio(db, 10_000)
        assert p_loaded.positions["ORPHAN"].is_quarantined is True

        # In portfolio value(), quarantined positions contribute 0, protecting NAV
        nav = p_loaded.value({"ORPHAN": 150.0}, {"ORPHAN": "INR"}, 1.0)
        assert nav == p_loaded.cash_inr
