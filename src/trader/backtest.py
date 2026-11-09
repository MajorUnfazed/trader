from dataclasses import dataclass
import numpy as np
import pandas as pd
from .config import Config
from .portfolio import Portfolio
from .strategies import dividend_quality, dividend_growth, long_term_growth, Signal, target_weights, fundamentals_as_of
from .calendars import valid_sessions

@dataclass
class BacktestResult:
    equity: pd.Series; portfolio: Portfolio; decisions: list[Signal]; snapshots: list[tuple]
    def metrics(self) -> dict:
        r=self.equity.pct_change().dropna(); years=max((self.equity.index[-1]-self.equity.index[0]).days/365.25,1/365.25)
        total=self.equity.iloc[-1]/self.equity.iloc[0]-1; peak=self.equity.cummax(); dd=(self.equity/peak-1).min()
        return {"total_return":total,"cagr":(1+total)**(1/years)-1,"max_drawdown":dd,"volatility":r.std()*np.sqrt(252),"sharpe":(r.mean()/r.std()*np.sqrt(252)) if r.std() else 0,"dividend_income_inr":self.portfolio.dividend_income_inr,"ending_value_inr":self.equity.iloc[-1]}

def benchmark_metrics(df: pd.DataFrame, start: str, end: str) -> dict:
    """Price-return benchmark metrics; use total-return series for dividend-inclusive comparisons."""
    h=df.loc[start:end,"adj_close"].dropna()
    if len(h)<2: raise ValueError("Benchmark has fewer than two observations")
    r=h.pct_change().dropna(); years=max((h.index[-1]-h.index[0]).days/365.25,1/365.25); total=h.iloc[-1]/h.iloc[0]-1
    return {"total_return":total,"cagr":(1+total)**(1/years)-1,"max_drawdown":(h/h.cummax()-1).min(),"volatility":r.std()*np.sqrt(252),"sharpe":r.mean()/r.std()*np.sqrt(252) if r.std() else 0}

def run_backtest(prices: dict[str,pd.DataFrame], fundamentals: pd.DataFrame, cfg: Config, start: str, end: str, rebalance_days=21, usd_inr: pd.Series | None = None, validate_calendars=True) -> BacktestResult:
    calendar_days=valid_sessions(prices,fundamentals,start,end) if validate_calendars else {}
    dates=sorted(set().union(*(set(days) for days in calendar_days.values()))) if calendar_days else sorted(set().union(*(set(df.loc[start:end].index) for df in prices.values())))
    if not dates: raise ValueError("No prices in selected period")
    metadata=fundamentals.sort_values("as_of_date").groupby("symbol",as_index=True).tail(1).set_index("symbol")
    if usd_inr is not None:
        usd_inr = usd_inr.sort_index().astype(float).reindex(pd.DatetimeIndex(dates), method="ffill")
        if usd_inr.isna().any(): raise ValueError("FX history must begin on or before the backtest start")
    def fx_at(day): return float(usd_inr.loc[day]) if usd_inr is not None else cfg.execution.usd_inr
    p=Portfolio(cfg.initial_capital_inr); equity=[]; decisions=[]; snapshots=[]; pending=None
    for i,day in enumerate(dates):
        quotes={s:float(df.loc[day,"close"]) for s,df in prices.items() if day in df.index}
        currencies={s:str(metadata.loc[s].get("currency","INR")) if s in metadata.index else "INR" for s in prices}
        # Each source's available rows define its exchange sessions. Positions
        # are marked at the last valid close between sessions, never at cost.
        for s,df in prices.items():
            if day in df.index: p.apply_split(s, float(df.loc[day,"split_factor"]) if "split_factor" in df else 1.0)
        p.pay_dividends(day.date())
        # Credit only holdings carried into the ex-dividend session, before any
        # opening rebalance can establish a new position.
        for s,df in prices.items():
            if day in df.index and float(df.loc[day,"dividend"]) > 0:
                payment = df.loc[day,"dividend_payment_date"] if "dividend_payment_date" in df else day
                payment = pd.Timestamp(payment).date() if pd.notna(payment) else day.date()
                p.declare_dividend(s,float(df.loc[day,"dividend"]),day.date(),payment,cfg.execution,fx_at(day))
        # Legacy files without payment dates pay on ex-date; explicit payment
        # dates remain receivables until their future payment session.
        p.pay_dividends(day.date())
        # Signals are formed from the completed prior session and intentionally
        # executed at this session's open: no same-close look-ahead execution.
        if pending is not None:
            open_quotes={s:float(df.loc[day,"open"]) for s,df in prices.items() if day in df.index}
            total=p.value(open_quotes,currencies,fx_at(day))
            wanted=set(pending)
            for s in list(p.positions):
                if s not in wanted and s in open_quotes: p.sell(s,open_quotes[s],cfg.execution,day.date(),"No longer selected")
            for symbol,(weight,reason) in pending.items():
                if symbol not in open_quotes: continue
                current=p.positions.get(symbol)
                current_value=(current.quantity*open_quotes[symbol]*p.fx(currencies[symbol],fx_at(day))) if current else 0
                target=total*weight
                sector=str(metadata.loc[symbol].get("sector","Unknown")) if symbol in metadata.index else "Unknown"
                sector_value=sum(pos.quantity*p.last_prices.get(sym,pos.avg_cost_native)*p.fx(currencies.get(sym,pos.currency),fx_at(day)) for sym,pos in p.positions.items() if pos.sector==sector)
                allowed=min(target-current_value, total*cfg.risk.max_sector_weight-sector_value)
                if allowed > total*cfg.risk.rebalance_threshold:
                    p.buy(symbol,open_quotes[symbol],currencies[symbol],sector,allowed,cfg.execution,day.date(),reason,fx_at(day))
            pending=None
        if i % rebalance_days == 0:
            available=fundamentals_as_of(fundamentals, day)
            signals=dividend_quality(available)+dividend_growth(available)+long_term_growth(prices,day)
            decisions.extend(signals)
            pending=target_weights(signals,cfg.allocations,cfg.risk.max_position_weight)
        value=p.value(quotes,currencies,fx_at(day)); equity.append((day,value))
        snapshots.append((day.date(),value,p.cash_inr,p.realised_pnl_inr,p.dividend_income_inr))
    return BacktestResult(pd.Series(dict(equity)).sort_index(),p,decisions,snapshots)
