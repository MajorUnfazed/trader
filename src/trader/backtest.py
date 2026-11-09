from dataclasses import dataclass
import numpy as np
import pandas as pd
from .config import Config
from .portfolio import Portfolio
from .strategies import dividend_quality, dividend_growth, long_term_growth, Signal, target_weights, fundamentals_as_of, universe_as_of
from .calendars import valid_sessions
from .risk import drift_targets, quantity_to_target


@dataclass
class BacktestResult:
    equity: pd.Series
    portfolio: Portfolio
    decisions: list[Signal]
    snapshots: list[tuple]
    universe_membership_used: bool = False

    def metrics(self) -> dict:
        r = self.equity.pct_change().dropna()
        years = max((self.equity.index[-1] - self.equity.index[0]).days / 365.25, 1 / 365.25)
        total = self.equity.iloc[-1] / self.equity.iloc[0] - 1
        peak = self.equity.cummax()
        dd = (self.equity / peak - 1).min()
        return {
            "total_return": total,
            "cagr": (1 + total) ** (1 / years) - 1,
            "max_drawdown": dd,
            "volatility": r.std() * np.sqrt(252),
            "sharpe": (r.mean() / r.std() * np.sqrt(252)) if r.std() else 0,
            "dividend_income_inr": self.portfolio.dividend_income_inr,
            "ending_value_inr": self.equity.iloc[-1],
            "universe_membership_used": self.universe_membership_used,
            "data_warning": None if self.universe_membership_used else "No historical universe membership supplied; results may contain survivorship bias.",
        }


def benchmark_metrics(df: pd.DataFrame, start: str, end: str) -> dict:
    """Price-return benchmark metrics; use total-return series for dividend-inclusive comparisons."""
    h = df.loc[start:end, "adj_close"].dropna()
    if len(h) < 2:
        raise ValueError("Benchmark has fewer than two observations")
    r = h.pct_change().dropna()
    years = max((h.index[-1] - h.index[0]).days / 365.25, 1 / 365.25)
    total = h.iloc[-1] / h.iloc[0] - 1
    return {
        "total_return": total,
        "cagr": (1 + total) ** (1 / years) - 1,
        "max_drawdown": (h / h.cummax() - 1).min(),
        "volatility": r.std() * np.sqrt(252),
        "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0,
    }


def run_backtest(
    prices: dict[str, pd.DataFrame],
    fundamentals: pd.DataFrame,
    cfg: Config,
    start: str,
    end: str,
    rebalance_days=21,
    usd_inr: pd.Series | None = None,
    validate_calendars=True,
    universe_membership: pd.DataFrame | None = None,
) -> BacktestResult:
    if rebalance_days < 1:
        raise ValueError("rebalance_days must be at least 1")
    calendar_days = valid_sessions(prices, fundamentals, start, end) if validate_calendars else {}
    dates = (
        sorted(set().union(*(set(days) for days in calendar_days.values())))
        if calendar_days
        else sorted(set().union(*(set(df.loc[start:end].index) for df in prices.values())))
    )
    if not dates:
        raise ValueError("No prices in selected period")

    if usd_inr is not None:
        usd_inr = usd_inr.sort_index().astype(float).reindex(pd.DatetimeIndex(dates), method="ffill")
        if usd_inr.isna().any():
            raise ValueError("FX history must begin on or before the backtest start")

    def fx_at(day):
        return float(usd_inr.loc[day]) if usd_inr is not None else cfg.execution.usd_inr

    p = Portfolio(cfg.initial_capital_inr)
    equity, decisions, snapshots = [], [], []
    # Orders are held per symbol until that symbol's next actual session.
    pending: dict[str, tuple[float, str, pd.Timestamp]] = {}

    for i, day in enumerate(dates):
        available = fundamentals_as_of(fundamentals, day)
        active_symbols = set(prices) if universe_membership is None else universe_as_of(universe_membership, day)
        inactive_held = set(p.positions) - active_symbols
        strategy_available = available.loc[available.index.intersection(active_symbols)]
        strategy_prices = {symbol: df for symbol, df in prices.items() if symbol in active_symbols}
        metadata = available
        currencies = {
            symbol: str(metadata.loc[symbol].get("currency", p.positions.get(symbol).currency if symbol in p.positions else "INR"))
            if symbol in metadata.index else p.positions.get(symbol).currency if symbol in p.positions else "INR"
            for symbol in prices
        }
        close_quotes = {
            symbol: float(df.loc[day, "close"])
            for symbol, df in prices.items()
            if day in df.index and ("tradable" not in df or bool(df.loc[day, "tradable"]))
        }
        fx = fx_at(day)

        # Corporate actions and dividend cashflows are processed before trading.
        for symbol, df in prices.items():
            if day in df.index:
                p.apply_split(symbol, float(df.loc[day, "split_factor"]) if "split_factor" in df else 1.0)
        p.pay_dividends(day.date(), fx)
        for symbol, df in prices.items():
            if day in df.index and float(df.loc[day, "dividend"]) > 0:
                payment = df.loc[day, "dividend_payment_date"] if "dividend_payment_date" in df else day
                payment = pd.Timestamp(payment).date() if pd.notna(payment) else day.date()
                p.declare_dividend(symbol, float(df.loc[day, "dividend"]), day.date(), payment, cfg.execution, fx)
        p.pay_dividends(day.date(), fx)

        # Execute each order only when its own exchange has an opening quote.
        open_quotes = {
            symbol: float(df.loc[day, "open"])
            for symbol, df in prices.items()
            if day in df.index and ("tradable" not in df or bool(df.loc[day, "tradable"]))
        }
        for symbol in list(pending):
            if symbol not in active_symbols and pending[symbol][0] > 0:
                pending.pop(symbol, None)
        for symbol in list(pending):
            target, reason, decision_day = pending[symbol]
            if day <= decision_day or symbol not in open_quotes:
                continue
            total = p.value(open_quotes, currencies, fx)
            position = p.positions.get(symbol)
            current_price = open_quotes[symbol]
            volume = float(prices[symbol].loc[day, "volume"]) if "volume" in prices[symbol].columns else None
            current_value = (
                position.quantity * current_price * p.fx(position.currency, fx)
                if position else 0.0
            )
            target_value = max(0.0, total * target)
            if current_value > target_value:
                qty = quantity_to_target(
                    position, current_price, p.fx(position.currency, fx), total, target
                )
                fill = p.sell(symbol, current_price, cfg.execution, day.date(), reason, quantity=qty, usd_inr=fx, volume=volume)
            elif target_value > current_value:
                sector = str(metadata.loc[symbol].get("sector", "Unknown")) if symbol in metadata.index else (position.sector if position else "Unknown")
                sector_value = sum(
                    pos.quantity * p.last_prices.get(sym, pos.avg_cost_native)
                    * p.fx(currencies.get(sym, pos.currency), fx)
                    for sym, pos in p.positions.items() if pos.sector == sector
                )
                budget = min(target_value - current_value, max(0.0, total * cfg.risk.max_sector_weight - sector_value))
                fill = p.buy(symbol, current_price, currencies[symbol], sector, budget, cfg.execution, day.date(), reason, fx, volume=volume)
            else:
                fill = None
            if fill is not None:
                pending.pop(symbol, None)

        # Re-evaluate strategy targets only on the configured rebalance cadence.
        if i % rebalance_days == 0:
            signals = dividend_quality(strategy_available) + dividend_growth(strategy_available) + long_term_growth(strategy_prices, day)
            decisions.extend(signals)
            if signals or inactive_held:
                targets = target_weights(signals, cfg.allocations, cfg.risk.max_position_weight)
                for held_symbol in p.positions:
                    if held_symbol not in targets:
                        reason = "Removed from historical universe" if held_symbol not in active_symbols else "Strategy exit: no qualifying signal"
                        targets[held_symbol] = (0.0, reason)
                for symbol, (weight, reason) in targets.items():
                    # Refresh the desired target while preserving the original
                    # decision timestamp so a closed exchange does not defer
                    # the same order indefinitely.
                    if symbol in pending:
                        pending[symbol] = (weight, reason, pending[symbol][2])
                    else:
                        pending[symbol] = (weight, reason, day)

        # Risk reductions are independent of strategy rebalances and never add exposure.
        p.value(close_quotes, currencies, fx)
        reductions = drift_targets(p, close_quotes, currencies, fx, cfg.risk)
        for symbol, (weight, reason) in reductions.items():
            prior = pending.get(symbol)
            if prior is None or weight < prior[0]:
                pending[symbol] = (weight, reason, day)

        value = p.value(close_quotes, currencies, fx)
        equity.append((day, value))
        snapshots.append((day.date(), value, p.cash_inr, p.realised_pnl_inr, p.dividend_income_inr))

    return BacktestResult(pd.Series(dict(equity)).sort_index(), p, decisions, snapshots, universe_membership is not None)
