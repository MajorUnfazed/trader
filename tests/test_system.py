import pandas as pd
from trader.config import Config
from trader.backtest import run_backtest
from trader.portfolio import Portfolio
from trader.config import ExecutionConfig
from trader.strategies import Signal, target_weights
from trader.storage import connect, persist_result, save_portfolio, load_portfolio
from trader.portfolio import Position
from trader.paper import run_paper
from trader.calendars import sessions
from trader.calendars import valid_sessions
from trader.risk import drift_targets
from trader.strategies import fundamentals_as_of
import pytest

def frame(start=100, dividend=0):
    idx=sessions("NSE","2022-01-01","2024-01-01")[:280]; price=pd.Series(range(start,start+280),index=idx,dtype=float)
    return pd.DataFrame({"open":price,"high":price,"low":price,"close":price,"adj_close":price,"volume":1_000,"dividend":dividend},index=idx)
def test_dividend_receivable_uses_payment_date_and_us_withholding():
    p=Portfolio(10000); cfg=ExecutionConfig(usd_inr=80,us_withholding_tax_rate=.25)
    p.buy("US",100,"USD","Tech",9000,cfg,pd.Timestamp("2022-01-01").date(),"test")
    event=p.declare_dividend("US",1,pd.Timestamp("2022-01-02").date(),pd.Timestamp("2022-01-05").date(),cfg)
    assert event.gross_inr == 80 and event.tax_inr == 20 and event.net_inr == 60
    assert p.pay_dividends(pd.Timestamp("2022-01-04").date()) == []
    p.pay_dividends(pd.Timestamp("2022-01-05").date(), 80)
    assert p.dividend_income_inr == 60

def test_dividend_entitlement_survives_partial_sale_and_multiple_events():
    p=Portfolio(50_000); cfg=ExecutionConfig(india_cost_bps=0,slippage_bps=0)
    p.buy("IN",100,"INR","Test",20_000,cfg,pd.Timestamp("2022-01-01").date(),"test")
    p.declare_dividend("IN",10,pd.Timestamp("2022-01-02").date(),pd.Timestamp("2022-01-10").date(),cfg)
    p.sell("IN",100,cfg,pd.Timestamp("2022-01-03").date(),"partial",quantity=100)
    p.declare_dividend("IN",20,pd.Timestamp("2022-01-04").date(),pd.Timestamp("2022-01-10").date(),cfg)
    paid=p.pay_dividends(pd.Timestamp("2022-01-10").date())
    assert len(paid) == 2 and p.dividend_income_inr == 4_000  # 200 shares then 100 shares

def test_fx_and_split_preserve_inr_cost_basis():
    p=Portfolio(20000); cfg=ExecutionConfig(usd_inr=80, us_cost_bps=0, slippage_bps=0)
    p.buy("US",100,"USD","Tech",16000,cfg,pd.Timestamp("2022-01-01").date(),"test",80)
    p.apply_split("US",2)
    assert p.positions["US"].quantity == 4 and p.positions["US"].avg_cost_native == 50
    assert p.value({"US":50},{"US":"USD"},90) == 22000

def test_buy_applies_slippage_and_transaction_cost_before_sizing():
    p=Portfolio(10_000); cfg=ExecutionConfig(india_cost_bps=100,slippage_bps=100)
    fill=p.buy("IN",100,"INR","Test",10_000,cfg,pd.Timestamp("2022-01-01").date(),"test")
    assert fill.quantity == 98 and fill.price_native == 101
    assert round(p.cash_inr,2) == round(10_000 - 98 * 101 * 1.01,2)

def test_strategy_targets_use_sleeves_and_position_cap():
    signals=[Signal("A","dividend_quality",9,""),Signal("B","dividend_growth",9,""),Signal("C","long_term_growth",9,"")]
    targets=target_weights(signals,{"dividend_quality":.3,"dividend_growth":.2,"long_term_growth":.3,"cash_reserve":.2},.1)
    assert all(weight <= .1 for weight,_ in targets.values())
    assert sum(weight for weight,_ in targets.values()) <= .3  # only one candidate per sleeve, capped per name

def test_overlapping_sleeves_do_not_breach_cap_and_leave_unallocatable_cash():
    signals=[Signal("A","dividend_quality",9,""),Signal("A","dividend_growth",9,""),Signal("A","long_term_growth",9,"")]
    targets=target_weights(signals,{"dividend_quality":.3,"dividend_growth":.2,"long_term_growth":.3,"cash_reserve":.2},.1)
    assert targets["A"][0] == .1 and sum(x[0] for x in targets.values()) == .1

def test_drift_targets_create_controlled_reduction_only():
    p=Portfolio(0); p.positions["A"]=__import__('trader.portfolio',fromlist=['Position']).Position("A",100,100,100,"INR","Tech")
    p.cash_inr=1_000; p.mark({"A":100})
    targets=drift_targets(p,{"A":100},{"A":"INR"},1,Config().risk)
    assert targets["A"][0] == Config().risk.max_position_weight
def test_backtest_runs_without_lookahead_crash():
    prices={"ABC":frame()}; f=pd.DataFrame([{"symbol":"ABC","as_of_date":"2021-12-31","dividend_yield":.03,"payout_ratio":.5,"fcf_positive":True,"debt_to_equity":.2,"revenue_growth":.1,"earnings_growth":.1,"dividend_growth_5y":.1,"sector":"Test","currency":"INR"}])
    result=run_backtest(prices,f,Config(),"2022-01-03","2023-02-01")
    assert result.equity.iloc[-1] > 0 and "cagr" in result.metrics()

def test_backtest_snapshots_persist_per_day_not_terminal_values():
    prices={"ABC":frame(dividend=1)}; f=pd.DataFrame([{"symbol":"ABC","as_of_date":"2021-12-31","dividend_yield":.03,"payout_ratio":.5,"fcf_positive":True,"debt_to_equity":.2,"dividend_growth_5y":.1,"sector":"Test","currency":"INR"}])
    result=run_backtest(prices,f,Config(),"2022-01-03","2023-02-01")
    db=connect(":memory:"); persist_result(result,db)
    rows=db.execute("SELECT count(*), min(cash_inr), max(cash_inr) FROM snapshots").fetchone()
    assert rows[0] == len(result.snapshots) and rows[1] != rows[2]

def test_stateful_paper_run_is_idempotent():
    prices={"ABC":frame()}; f=pd.DataFrame([{"symbol":"ABC","as_of_date":"2021-12-31","dividend_yield":.03,"payout_ratio":.5,"fcf_positive":True,"debt_to_equity":.2,"revenue_growth":.1,"earnings_growth":.1,"dividend_growth_5y":.1,"sector":"Test","currency":"INR"}])
    db=connect(":memory:")
    first=run_paper(prices,f,Config(),db); orders=db.execute("SELECT count(*) FROM orders").fetchone()[0]
    second=run_paper(prices,f,Config(),db)
    assert first["processed"] == 280 and second["processed"] == 0
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == orders

def test_paper_dividend_event_is_idempotent_across_repeated_runs():
    prices={"ABC":frame()}; ex_date=prices["ABC"].index[2]; pay_date=prices["ABC"].index[5]
    prices["ABC"].loc[ex_date,"dividend"]=5; prices["ABC"]["dividend_payment_date"]=pd.NaT; prices["ABC"].loc[ex_date,"dividend_payment_date"]=pay_date
    f=pd.DataFrame([{"symbol":"ABC","as_of_date":"2021-12-31","dividend_yield":.03,"payout_ratio":.5,"fcf_positive":True,"debt_to_equity":.2,"dividend_growth_5y":.1,"sector":"Test","currency":"INR","exchange":"NSE"}])
    db=connect(":memory:"); p=Portfolio(0); p.positions["ABC"]=Position("ABC",10,100,100,"INR","Test")
    save_portfolio(db,p,str((prices["ABC"].index[0]-pd.Timedelta(days=1)).date())); db.commit()
    first=run_paper(prices,f,Config(),db); second=run_paper(prices,f,Config(),db)
    assert first["portfolio"].dividend_income_inr == 50 and second["processed"] == 0
    assert db.execute("SELECT count(*) FROM dividend_receivables WHERE status='PAID'").fetchone()[0] == 1

def test_calendar_rejects_holiday_rows_and_missing_session_quotes():
    f=pd.DataFrame([{"symbol":"ABC","as_of_date":"2021-12-31","currency":"INR","exchange":"NSE"}])
    good=frame(); weekend=good.copy(); weekend.loc[pd.Timestamp("2022-01-01")]=weekend.iloc[0]
    with pytest.raises(ValueError,match="non-session"): valid_sessions({"ABC":weekend},f,"2022-01-01",good.index[-1])
    with pytest.raises(ValueError,match="missing quotes"): valid_sessions({"ABC":good.drop(good.index[10])},f,good.index[0],good.index[-1])

def test_last_known_price_is_used_for_missing_quote_not_cost_basis():
    p=Portfolio(0); p.positions["A"]=__import__('trader.portfolio',fromlist=['Position']).Position("A",1,100,100,"INR","Test")
    assert p.value({"A":150},{"A":"INR"},1) == 150
    assert p.value({}, {"A":"INR"},1) == 150

def test_fundamentals_are_latest_strictly_as_of_decision_date():
    f=pd.DataFrame([{"symbol":"A","as_of_date":"2022-01-01","payout_ratio":.4},{"symbol":"A","as_of_date":"2022-07-01","payout_ratio":.9},{"symbol":"B","as_of_date":"2022-07-01","payout_ratio":.2}])
    selected=fundamentals_as_of(f,"2022-06-01")
    assert list(selected.index) == ["A"] and selected.loc["A","payout_ratio"] == .4


def test_zero_quantity_sell_is_a_noop_and_negative_quantity_is_rejected():
    p = Portfolio(20_000)
    cfg = ExecutionConfig(india_cost_bps=0, slippage_bps=0)
    p.buy("IN", 100, "INR", "Test", 10_000, cfg, pd.Timestamp("2022-01-01").date(), "seed")
    before_cash = p.cash_inr
    before_quantity = p.positions["IN"].quantity
    assert p.sell("IN", 100, cfg, pd.Timestamp("2022-01-02").date(), "zero", quantity=0) is None
    assert p.cash_inr == before_cash
    assert p.positions["IN"].quantity == before_quantity
    with pytest.raises(ValueError, match="negative"):
        p.sell("IN", 100, cfg, pd.Timestamp("2022-01-02").date(), "negative", quantity=-1)


def test_pending_dividend_is_in_equity_and_revalues_at_payment_fx():
    p = Portfolio(20_000)
    cfg = ExecutionConfig(usd_inr=80, us_withholding_tax_rate=0.25, us_cost_bps=0, slippage_bps=0)
    p.buy("US", 100, "USD", "Tech", 8_000, cfg, pd.Timestamp("2022-01-01").date(), "seed", 80)
    event = p.declare_dividend("US", 1, pd.Timestamp("2022-01-02").date(), pd.Timestamp("2022-01-05").date(), cfg, 80)
    before_payment = p.value({}, {"US": "USD"}, 90)
    assert before_payment == 20_000 - 8_000 + 9_000 + 67.5
    paid = p.pay_dividends(pd.Timestamp("2022-01-05").date(), 90)
    assert len(paid) == 1
    assert p.cash_inr == 12_067.5
    assert p.value({}, {"US": "USD"}, 90) == before_payment
    assert p.dividend_income_inr == 67.5


def test_currency_receivable_survives_sqlite_round_trip():
    p = Portfolio(10_000)
    cfg = ExecutionConfig(usd_inr=80, us_withholding_tax_rate=0.25, us_cost_bps=0, slippage_bps=0)
    p.positions["US"] = Position("US", 2, 100, 16_000, "USD", "Tech")
    p.declare_dividend("US", 1, pd.Timestamp("2022-01-02").date(), pd.Timestamp("2022-01-10").date(), cfg, 80)
    db = connect(":memory:")
    save_portfolio(db, p, "2022-01-02")
    db.commit()
    restored, _ = load_portfolio(db, 10_000)
    receivable = restored.dividend_receivables[0]
    assert receivable.currency == "USD"
    assert receivable.net_native == 1.5
    assert restored.value({}, {"US": "USD"}, 90) == 10_000 + 2 * 100 * 90 + 1.5 * 90


def test_backtest_keeps_orders_until_the_symbol_next_has_a_session():
    day1 = pd.Timestamp("2024-01-02")
    day2 = pd.Timestamp("2024-01-03")
    day3 = pd.Timestamp("2024-01-04")

    def prices_for(days):
        values = pd.Series([100.0] * len(days), index=days)
        return pd.DataFrame({
            "open": values, "high": values, "low": values, "close": values,
            "adj_close": values, "volume": 20_000, "dividend": 0.0,
        }, index=days)

    prices = {
        "A": prices_for(pd.DatetimeIndex([day1, day2])),
        "B": prices_for(pd.DatetimeIndex([day1, day3])),
    }
    fundamentals = pd.DataFrame([
        {"symbol": symbol, "as_of_date": "2024-01-01", "dividend_yield": .03,
         "payout_ratio": .5, "fcf_positive": True, "debt_to_equity": .2,
         "dividend_growth_5y": .1, "sector": "Test", "currency": "INR"}
        for symbol in ("A", "B")
    ])
    result = run_backtest(prices, fundamentals, Config(), str(day1.date()), str(day3.date()),
                          rebalance_days=1, validate_calendars=False)
    fills_b = [fill for fill in result.portfolio.fills if fill.symbol == "B" and fill.side == "BUY"]
    assert len(fills_b) == 1
    assert fills_b[0].date == day3.date()


def test_empty_signal_set_does_not_liquidate_paper_holdings():
    prices = {"ABC": frame()}
    prices["ABC"]["adj_close"] = 100.0
    prices["ABC"]["close"] = 100.0
    fundamentals = pd.DataFrame([{
        "symbol": "ABC", "as_of_date": "2021-12-31", "dividend_yield": 0.0,
        "payout_ratio": .5, "fcf_positive": False, "debt_to_equity": .2,
        "dividend_growth_5y": 0.0, "sector": "Test", "currency": "INR", "exchange": "NSE",
    }])
    db = connect(":memory:")
    p = Portfolio(9_000)
    p.positions["ABC"] = Position("ABC", 10, 100, 100, "INR", "Test")
    save_portfolio(db, p, str((prices["ABC"].index[0] - pd.Timedelta(days=1)).date()))
    db.commit()
    run_paper(prices, fundamentals, Config(), db)
    sells = db.execute("SELECT count(*) FROM orders WHERE side='SELL'").fetchone()[0]
    assert sells == 0


def test_paper_order_waits_for_the_symbol_next_session():
    day1 = pd.Timestamp("2024-01-02")
    day2 = pd.Timestamp("2024-01-03")
    day3 = pd.Timestamp("2024-01-04")

    def prices_for(days):
        values = pd.Series([100.0] * len(days), index=days)
        return pd.DataFrame({
            "open": values, "high": values, "low": values, "close": values,
            "adj_close": values, "volume": 20_000, "dividend": 0.0,
        }, index=days)

    prices = {
        "A": prices_for(pd.DatetimeIndex([day1, day2, day3])),
        "B": prices_for(pd.DatetimeIndex([day1, day3])),
    }
    fundamentals = pd.DataFrame([{
        "symbol": "B", "as_of_date": "2024-01-01", "dividend_yield": .03,
        "payout_ratio": .5, "fcf_positive": True, "debt_to_equity": .2,
        "dividend_growth_5y": .1, "sector": "Test", "currency": "INR",
    }])
    db = connect(":memory:")
    first = run_paper(prices, fundamentals, Config(), db, as_of=day2, validate_calendars=False)
    pending = db.execute("SELECT count(*) FROM orders WHERE symbol='B' AND side='BUY' AND status='PENDING'").fetchone()[0]
    assert pending == 1
    second = run_paper(prices, fundamentals, Config(), db, as_of=day3, validate_calendars=False)
    fills = db.execute("SELECT count(*) FROM fills WHERE symbol='B' AND side='BUY'").fetchone()[0]
    assert second["processed"] == 1
    assert fills == 1
