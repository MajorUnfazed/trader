import sqlite3
import pandas as pd
import pytest

from trader.config import Config
from trader.portfolio import Portfolio, Position
from trader.storage import (
    open_db,
    load_portfolio,
    save_portfolio,
    recover_interrupted_sessions,
    ORDER_STATUS_PENDING,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_EXPIRED,
)
from trader.paper import run_paper
from trader.calendars import sessions


def make_test_frame(start_price=100.0, length=10, dividend=0.0, volume=100_000):
    idx = sessions("NSE", "2023-01-01", "2023-03-01")[:length]
    price = pd.Series([start_price + i for i in range(len(idx))], index=idx, dtype=float)
    return pd.DataFrame({
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "adj_close": price,
        "volume": volume,
        "dividend": dividend,
    }, index=idx)


def test_order_lifecycle_transitions_and_status_checks(tmp_path):
    db_path = str(tmp_path / "lifecycle.db")
    with open_db(db_path) as db:
        # Valid insert
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES('2023-01-02', 'INFY', 'BUY', 'MARKET', 0.1, 10, 'DAY', 'Test', 'PENDING')"""
        )
        oid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Valid transition PENDING -> FILLED
        db.execute("UPDATE orders SET status='FILLED', filled_date='2023-01-02', filled_quantity=10 WHERE id=?", (oid,))
        status = db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0]
        assert status == ORDER_STATUS_FILLED

        # Invalid status must be rejected by CHECK constraint
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE orders SET status='INVALID_STATUS' WHERE id=?", (oid,))

        # Foreign key enforcement on fills
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO fills(order_id, date, symbol, side, quantity, price_native, fee_inr, reason) "
                "VALUES(999999, '2023-01-02', 'INFY', 'BUY', 10, 100.0, 1.0, 'Invalid FK')"
            )


def test_pending_order_restart_survival_and_execution(tmp_path):
    db_path = str(tmp_path / "restart.db")
    prices = {"INFY": make_test_frame(start_price=100.0, length=5)}
    f = pd.DataFrame([{
        "symbol": "INFY", "as_of_date": "2022-12-31", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.1,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    first_day = prices["INFY"].index[0]
    second_day = prices["INFY"].index[1]

    # Queue an order into the DB and shut down the connection
    with open_db(db_path) as db:
        p = Portfolio(100_000)
        save_portfolio(db, p, str(first_day.date()))
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES(?, 'INFY', 'BUY', 'MARKET', 0.10, 50, 'DAY', 'Test rebalance', 'PENDING')""",
            (str(first_day.date()),)
        )
        db.commit()

    # Restart application: reopen DB and verify order survived
    with open_db(db_path) as db:
        pending = db.execute("SELECT id, status FROM orders WHERE status='PENDING'").fetchall()
        assert len(pending) == 1
        oid = pending[0][0]

        # Run paper on second_day: order executes
        run_paper(prices, f, Config(), db, as_of=str(second_day.date()))

        # Verify order transitioned to FILLED and fill was recorded
        order_row = db.execute("SELECT status, filled_quantity FROM orders WHERE id=?", (oid,)).fetchone()
        assert order_row[0] == ORDER_STATUS_FILLED
        assert order_row[1] > 0

        fill_count = db.execute("SELECT count(*) FROM fills WHERE order_id=?", (oid,)).fetchone()[0]
        assert fill_count == 1

        # Run third session: verify order does not execute again
        third_day = prices["INFY"].index[2]
        run_paper(prices, f, Config(), db, as_of=str(third_day.date()))
        fill_count_after = db.execute("SELECT count(*) FROM fills WHERE order_id=?", (oid,)).fetchone()[0]
        assert fill_count_after == 1


def test_zombie_order_cancellation_when_disqualified(tmp_path):
    db_path = str(tmp_path / "zombie.db")
    # Low volume keeps the GTC order from filling at open, so it reaches rebalancing at close
    prices = {"ABC": make_test_frame(start_price=100.0, length=2, volume=10)}
    # ABC has no qualifying signals
    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.0,
        "payout_ratio": 1.5, "fcf_positive": False, "debt_to_equity": 5.0,
        "revenue_growth": -0.1, "earnings_growth": -0.1, "dividend_growth_5y": 0.0,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    with open_db(db_path) as db:
        # Pre-seed a pending BUY order for ABC (zombie candidate)
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES('2023-01-01', 'ABC', 'BUY', 'MARKET', 0.10, 10, 'GTC', 'Prior signal', 'PENDING')"""
        )
        db.commit()

        # Run paper session on ABC
        run_paper(prices, f, Config(), db)

        # The pending BUY order must be cancelled because ABC is not in strategy targets
        order = db.execute("SELECT status, cancellation_reason FROM orders WHERE symbol='ABC'").fetchone()
        assert order[0] == ORDER_STATUS_CANCELLED
        assert "no longer in strategy targets" in order[1] or "No qualifying signals" in order[1]


def test_day_order_expiration_when_unfilled_due_to_liquidity(tmp_path):
    db_path = str(tmp_path / "day_expiry.db")
    # Low volume that blocks execution under 1% participation cap
    prices = {"ABC": make_test_frame(start_price=100.0, length=5, volume=10)}
    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.04,
        "payout_ratio": 0.4, "fcf_positive": True, "debt_to_equity": 0.1,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.1,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    with open_db(db_path) as db:
        first_day = prices["ABC"].index[0]
        second_day = prices["ABC"].index[1]
        save_portfolio(db, Portfolio(100_000), str(first_day.date()))

        # Queue a DAY order that requires more volume than 1% of 10 shares (0 shares allowed)
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES(?, 'ABC', 'BUY', 'MARKET', 0.10, 50, 'DAY', 'Target allocation', 'PENDING')""",
            (str(first_day.date()),)
        )
        db.commit()

        # Run second_day
        run_paper(prices, f, Config(), db, as_of=str(second_day.date()))

        # At the end of the DAY session, the unfilled order must expire
        order = db.execute("SELECT status, cancellation_reason FROM orders WHERE symbol='ABC'").fetchone()
        assert order[0] == ORDER_STATUS_EXPIRED
        assert "end of DAY trading session" in order[1]


def test_gtc_order_expiration_uses_trading_days(tmp_path):
    db_path = str(tmp_path / "gtc_expiry.db")
    # Volume 10 blocks execution
    prices = {"ABC": make_test_frame(start_price=100.0, length=10, volume=10)}
    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.04,
        "payout_ratio": 0.4, "fcf_positive": True, "debt_to_equity": 0.1,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.1,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    cfg = Config()
    cfg.paper.gtc_expiration_trading_days = 3

    with open_db(db_path) as db:
        p = Portfolio(100_000)
        day0 = prices["ABC"].index[0]
        save_portfolio(db, p, str(day0.date()))
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES(?, 'ABC', 'BUY', 'MARKET', 0.10, 50, 'GTC', 'Long term buy', 'PENDING')""",
            (str(day0.date()),)
        )
        db.commit()

        # Run day 1: order should remain PENDING
        day1 = prices["ABC"].index[1]
        run_paper(prices, f, cfg, db, as_of=str(day1.date()))
        assert db.execute("SELECT status FROM orders").fetchone()[0] == ORDER_STATUS_PENDING

        # Run day 2: order should remain PENDING
        day2 = prices["ABC"].index[2]
        run_paper(prices, f, cfg, db, as_of=str(day2.date()))
        assert db.execute("SELECT status FROM orders").fetchone()[0] == ORDER_STATUS_PENDING

        # Run day 3: reaches 3 trading days and expires
        day3 = prices["ABC"].index[3]
        run_paper(prices, f, cfg, db, as_of=str(day3.date()))
        order = db.execute("SELECT status, cancellation_reason FROM orders").fetchone()
        assert order[0] == ORDER_STATUS_EXPIRED
        assert "GTC trading day limit reached" in order[1]


def test_partial_fill_handling_and_remainder_expiration(tmp_path):
    db_path = str(tmp_path / "partial_fill.db")
    # Volume 5000 -> 1% is 50 shares
    prices = {"ABC": make_test_frame(start_price=100.0, length=5, volume=5000)}
    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.04,
        "payout_ratio": 0.4, "fcf_positive": True, "debt_to_equity": 0.1,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.1,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    cfg = Config()
    cfg.paper.allow_partial_fills = True

    with open_db(db_path) as db:
        p = Portfolio(100_000)
        day0 = prices["ABC"].index[0]
        day1 = prices["ABC"].index[1]
        save_portfolio(db, p, str(day0.date()))

        # Need budget for 100 shares, but volume cap limits to 50 shares
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES(?, 'ABC', 'BUY', 'MARKET', 0.20, 100, 'DAY', 'Target allocation', 'PENDING')""",
            (str(day0.date()),)
        )
        db.commit()

        run_paper(prices, f, cfg, db, as_of=str(day1.date()))

        # Check order state: filled 50, remaining 50 expired as DAY order
        order = db.execute("SELECT status, filled_quantity, cancellation_reason FROM orders").fetchone()
        assert order[0] == ORDER_STATUS_EXPIRED
        assert order[1] == 50
        assert "Partially filled" in order[2]

        fill = db.execute("SELECT quantity FROM fills").fetchone()
        assert fill[0] == 50


def test_dividend_idempotency_across_restarts_and_float_precision(tmp_path):
    db_path = str(tmp_path / "dividend_idem.db")
    prices = {"ABC": make_test_frame(start_price=100.0, length=6)}
    ex_date = prices["ABC"].index[1]
    pay_date = prices["ABC"].index[3]
    prices["ABC"].loc[ex_date, "dividend"] = 5.123456789
    prices["ABC"]["dividend_payment_date"] = pd.NaT
    prices["ABC"].loc[ex_date, "dividend_payment_date"] = pay_date

    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "dividend_growth_5y": 0.1, "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    with open_db(db_path) as db:
        p = Portfolio(10_000)
        p.positions["ABC"] = Position("ABC", 100, 100.0, 100.0, "INR", "Tech")
        save_portfolio(db, p, str((prices["ABC"].index[0] - pd.Timedelta(days=1)).date()))
        db.commit()

        # Run through payment date
        report1 = run_paper(prices, f, Config(), db, as_of=str(pay_date.date()))
        div1 = report1["portfolio"].dividend_income_inr
        assert div1 > 0

        # Verify DB status is PAID and actual_paid_inr is stored
        row = db.execute("SELECT status, actual_paid_inr FROM dividend_receivables WHERE symbol='ABC'").fetchone()
        assert row[0] == "PAID"
        assert row[1] is not None

        # Re-run: ensure dividend is NOT credited again
        report2 = run_paper(prices, f, Config(), db, as_of=str(pay_date.date()))
        assert report2["processed"] == 0

        paid_count = db.execute("SELECT count(*) FROM dividend_receivables WHERE status='PAID'").fetchone()[0]
        assert paid_count == 1


def test_delisting_settlement_at_zero_price_bankruptcy(tmp_path):
    db_path = str(tmp_path / "delist_zero.db")
    prices = {"BROKE": make_test_frame(start_price=10.0, length=5)}
    delist_date = prices["BROKE"].index[2]
    prices["BROKE"]["delisting_price"] = float("nan")
    prices["BROKE"].loc[delist_date, "delisting_price"] = 0.0

    f = pd.DataFrame([{
        "symbol": "BROKE", "as_of_date": "2022-12-31", "dividend_yield": 0.0,
        "payout_ratio": 0.0, "fcf_positive": False, "debt_to_equity": 10.0,
        "revenue_growth": -0.5, "earnings_growth": -0.5, "dividend_growth_5y": 0.0,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    with open_db(db_path) as db:
        p = Portfolio(50_000)
        p.positions["BROKE"] = Position("BROKE", 100, 10.0, 10.0, "INR", "Tech")
        day0 = prices["BROKE"].index[0]
        save_portfolio(db, p, str((day0 - pd.Timedelta(days=1)).date()))

        # Pre-seed a pending order for BROKE created for the delist date
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES(?, 'BROKE', 'BUY', 'MARKET', 0.10, 10, 'GTC', 'Test', 'PENDING')""",
            (str(delist_date.date()),)
        )
        db.commit()

        # Run paper: should NOT raise ValueError, should write off position at 0
        report = run_paper(prices, f, Config(), db, as_of=str(delist_date.date()))
        p_final = report["portfolio"]

        # Position should be wiped out
        assert "BROKE" not in p_final.positions
        # Cash should not increase
        assert p_final.cash_inr == 50_000
        # Realised loss should be recorded
        assert p_final.realised_pnl_inr == -1000.0

        # Pending orders for BROKE must be CANCELLED
        order = db.execute("SELECT status, cancellation_reason FROM orders WHERE symbol='BROKE' AND side='BUY'").fetchone()
        assert order[0] == ORDER_STATUS_CANCELLED
        assert "Security delisted" in order[1]


def test_crash_recovery_for_running_sessions(tmp_path):
    db_path = str(tmp_path / "crash_recovery.db")
    with open_db(db_path) as db:
        # Simulate a prior session that was left in RUNNING status due to abrupt termination
        db.execute(
            """INSERT INTO paper_sessions(run_id, session_date, started_at, config_hash, status)
               VALUES('orphan-run-123', '2023-01-05', CURRENT_TIMESTAMP, 'hash123', 'RUNNING')"""
        )
        db.commit()

        # Call recover_interrupted_sessions
        recovered = recover_interrupted_sessions(db)
        assert recovered == 1

        row = db.execute("SELECT status, error_message, completed_at FROM paper_sessions WHERE run_id='orphan-run-123'").fetchone()
        assert row[0] == "FAILED"
        assert "Interrupted" in row[1]
        assert row[2] is not None


def test_transaction_rollback_and_durable_failure_record(tmp_path):
    db_path = str(tmp_path / "tx_rollback.db")
    prices = {"ABC": make_test_frame(start_price=100.0, length=5)}
    f = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2022-12-31", "dividend_yield": 0.03,
        "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2,
        "revenue_growth": 0.1, "earnings_growth": 0.1, "dividend_growth_5y": 0.1,
        "sector": "Tech", "currency": "INR", "exchange": "NSE"
    }])

    # Corrupt price data on day 2 to force ValueError during simulation loop
    bad_prices = {"ABC": prices["ABC"].copy()}
    bad_prices["ABC"]["delisting_price"] = float("nan")
    bad_prices["ABC"].loc[bad_prices["ABC"].index[1], "delisting_price"] = -50.0  # Invalid negative price

    with open_db(db_path) as db:
        p = Portfolio(100_000)
        day0 = bad_prices["ABC"].index[0]
        save_portfolio(db, p, str((day0 - pd.Timedelta(days=1)).date()))
        db.commit()

        with pytest.raises(ValueError, match="Invalid delisting settlement"):
            run_paper(bad_prices, f, Config(), db, as_of=str(bad_prices["ABC"].index[2].date()))

        # Verify portfolio state was NOT mutated/saved
        loaded_p, last_date = load_portfolio(db, 100_000)
        assert last_date == str((day0 - pd.Timedelta(days=1)).date())
        assert loaded_p.cash_inr == 100_000

        # Verify paper_sessions preserved the FAILED record with error message
        session = db.execute("SELECT status, error_message FROM paper_sessions ORDER BY id DESC LIMIT 1").fetchone()
        assert session[0] == "FAILED"
        assert "Invalid delisting settlement" in session[1]


def test_database_migration_from_legacy_schema(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    # Manually construct legacy v0 database
    raw_conn = sqlite3.connect(db_path)
    raw_conn.executescript("""
        CREATE TABLE fills (id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, side TEXT, quantity INTEGER, price_native REAL, fee_inr REAL, reason TEXT);
        CREATE TABLE snapshots (date TEXT PRIMARY KEY, value_inr REAL, cash_inr REAL, realised_pnl_inr REAL, dividend_income_inr REAL);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, strategy TEXT, score REAL, explanation TEXT);
        CREATE TABLE portfolio_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), cash_inr REAL NOT NULL, realised_pnl_inr REAL NOT NULL, dividend_income_inr REAL NOT NULL, last_processed_date TEXT);
        CREATE TABLE positions (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, avg_cost_native REAL NOT NULL, avg_cost_inr REAL NOT NULL, currency TEXT NOT NULL, sector TEXT NOT NULL, last_price_native REAL);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, created_date TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, target_weight REAL, reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','FILLED','CANCELLED')), filled_date TEXT);
        CREATE TABLE dividend_receivables (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, payment_date TEXT NOT NULL, net_inr REAL NOT NULL, gross_inr REAL NOT NULL, tax_inr REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','PAID')));
        CREATE TABLE paper_sessions (date TEXT PRIMARY KEY, completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    """)

    # Insert legacy rows
    raw_conn.execute("INSERT INTO orders(created_date, symbol, side, target_weight, reason, status) VALUES('2022-01-01', 'LEGACY', 'BUY', 0.1, 'Old order', 'PENDING')")
    raw_conn.execute("INSERT INTO dividend_receivables(symbol, payment_date, net_inr, gross_inr, tax_inr, status) VALUES('LEGACY', '2022-01-10', 100, 100, 0, 'PENDING')")
    raw_conn.execute("INSERT INTO paper_sessions(date) VALUES('2022-01-01')")
    raw_conn.commit()
    raw_conn.close()

    # Now open with hardened connect() which triggers migrate_db
    with open_db(db_path) as db:
        # Check user_version is 2
        ver = db.execute("PRAGMA user_version").fetchone()[0]
        assert ver == 2

        # Verify legacy order preserved and has new defaults
        order = db.execute("SELECT symbol, status, order_type, time_in_force FROM orders WHERE symbol='LEGACY'").fetchone()
        assert order[0] == "LEGACY"
        assert order[1] == "PENDING"
        assert order[2] == "MARKET"
        assert order[3] == "DAY"

        # Verify legacy dividend receivable preserved with ex_date migrated
        div = db.execute("SELECT symbol, ex_date, payment_date, net_inr, currency FROM dividend_receivables WHERE symbol='LEGACY'").fetchone()
        assert div[0] == "LEGACY"
        assert div[1] == "2022-01-10"
        assert div[2] == "2022-01-10"
        assert div[3] == 100.0
        assert div[4] == "INR"

        # Verify legacy paper session preserved with synthetic run_id
        session = db.execute("SELECT run_id, session_date, status FROM paper_sessions WHERE session_date='2022-01-01'").fetchone()
        assert session[0] == "legacy-2022-01-01"
        assert session[1] == "2022-01-01"
        assert session[2] == "SUCCESS"

        # Verify new indexes exist
        indexes = {row[1] for row in db.execute("PRAGMA index_list('orders')").fetchall()}
        assert "idx_orders_status" in indexes


def test_open_db_connection_lifecycle_closes_properly(tmp_path):
    db_path = str(tmp_path / "conn_close.db")
    captured_conn = None
    with open_db(db_path) as db:
        captured_conn = db
        db.execute("SELECT 1")

    # Outside the context manager, the connection must be closed
    with pytest.raises(sqlite3.ProgrammingError, match="Cannot operate on a closed database"):
        captured_conn.execute("SELECT 1")


def test_multi_exchange_holiday_isolation_for_pending_orders(tmp_path):
    db_path = str(tmp_path / "multi_exchange.db")
    # 2023-01-16 is Martin Luther King Jr. Day: NYSE closed, NSE open
    nse_days = sessions("NSE", "2023-01-13", "2023-01-18")
    nyse_days = sessions("NYSE", "2023-01-13", "2023-01-18")
    assert pd.Timestamp("2023-01-16") in nse_days
    assert pd.Timestamp("2023-01-16") not in nyse_days

    in_series = pd.Series(100.0, index=nse_days)
    us_series = pd.Series(50.0, index=nyse_days)
    prices = {
        "IN_STOCK": pd.DataFrame({"open": in_series, "high": in_series, "low": in_series, "close": in_series, "adj_close": in_series, "volume": 100_000, "dividend": 0.0}, index=nse_days),
        "US_STOCK": pd.DataFrame({"open": us_series, "high": us_series, "low": us_series, "close": us_series, "adj_close": us_series, "volume": 100_000, "dividend": 0.0}, index=nyse_days),
    }
    f = pd.DataFrame([
        {"symbol": "IN_STOCK", "as_of_date": "2022-12-31", "dividend_yield": 0.03, "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2, "sector": "Tech", "currency": "INR", "exchange": "NSE"},
        {"symbol": "US_STOCK", "as_of_date": "2022-12-31", "dividend_yield": 0.03, "payout_ratio": 0.5, "fcf_positive": True, "debt_to_equity": 0.2, "sector": "Tech", "currency": "USD", "exchange": "NYSE"},
    ])

    with open_db(db_path) as db:
        p = Portfolio(100_000)
        day_fri = pd.Timestamp("2023-01-13")
        save_portfolio(db, p, str(day_fri.date()))

        # Pre-seed a pending DAY order for US_STOCK queued on Friday
        db.execute(
            """INSERT INTO orders(created_date, symbol, side, order_type, target_weight,
                                 requested_quantity, time_in_force, reason, status)
               VALUES('2023-01-13', 'US_STOCK', 'BUY', 'MARKET', 0.10, 10, 'DAY', 'US allocation', 'PENDING')"""
        )
        db.commit()

        # Run paper on Monday (2023-01-16): NSE open, NYSE closed
        run_paper(prices, f, Config(), db, as_of="2023-01-16")

        # The US_STOCK order must STILL BE PENDING (not expired, not filled) because NYSE was closed!
        order = db.execute("SELECT status FROM orders WHERE symbol='US_STOCK'").fetchone()
        assert order[0] == ORDER_STATUS_PENDING

        # Now run paper on Tuesday (2023-01-17): NYSE is open
        run_paper(prices, f, Config(), db, as_of="2023-01-17")

        # Now US_STOCK order should be FILLED
        order_tue = db.execute("SELECT status FROM orders WHERE symbol='US_STOCK'").fetchone()
        assert order_tue[0] == ORDER_STATUS_FILLED
