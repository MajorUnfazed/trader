"""Stateful, idempotent paper execution; never connects to a broker."""
import pandas as pd
from .portfolio import Portfolio
from .strategies import dividend_quality, dividend_growth, long_term_growth, target_weights, fundamentals_as_of
from .storage import load_portfolio, save_portfolio, insert_snapshot
from .calendars import valid_sessions
from .risk import drift_targets, quantity_to_target


def run_paper(prices, fundamentals, cfg, connection, as_of=None, usd_inr=None, validate_calendars=True):
    requested_end = pd.Timestamp(as_of) if as_of else max(x.index.max() for x in prices.values())
    calendar_days = valid_sessions(prices, fundamentals, min(x.index.min() for x in prices.values()), requested_end) if validate_calendars else {}
    all_dates = sorted(set().union(*(set(days) for days in calendar_days.values()))) if calendar_days else sorted(set().union(*(set(x.index) for x in prices.values())))
    if not all_dates:
        raise ValueError("No price data")
    p, last = load_portfolio(connection, cfg.initial_capital_inr)
    dates = [d for d in all_dates if d <= requested_end and (last is None or str(d.date()) > last)]
    if not dates:
        return {"processed": 0, "portfolio": p}
    if usd_inr is not None:
        usd_inr = usd_inr.sort_index().reindex(pd.DatetimeIndex(dates), method="ffill")
        if usd_inr.isna().any():
            raise ValueError("FX history must begin before paper sessions")

    def fx_at(day):
        return float(usd_inr.loc[day]) if usd_inr is not None else cfg.execution.usd_inr

    for day in dates:
        fx = fx_at(day)
        metadata = fundamentals_as_of(fundamentals, day)
        currencies = {
            s: str(metadata.loc[s].get("currency", p.positions[s].currency if s in p.positions else "INR"))
            if s in metadata.index else (p.positions[s].currency if s in p.positions else "INR")
            for s in prices
        }
        close = {s: float(x.loc[day, "close"]) for s, x in prices.items()
                 if day in x.index and ("tradable" not in x or bool(x.loc[day, "tradable"]))}
        open_quotes = {s: float(x.loc[day, "open"]) for s, x in prices.items()
                       if day in x.index and ("tradable" not in x or bool(x.loc[day, "tradable"]))}

        for symbol, frame in prices.items():
            if day in frame.index:
                p.apply_split(symbol, float(frame.loc[day, "split_factor"]) if "split_factor" in frame else 1)
        paid = p.pay_dividends(day.date(), fx)
        for event in paid:
            connection.execute(
                "UPDATE dividend_receivables SET status='PAID' WHERE symbol=? AND ex_date=? AND payment_date=? AND net_inr=?",
                (event.symbol, str(event.ex_date), str(event.payment_date), event.net_inr),
            )

        # Determine ex-date entitlement from shares held coming into the session.
        for symbol, frame in prices.items():
            if day in frame.index and float(frame.loc[day, "dividend"]) > 0:
                payment = frame.loc[day, "dividend_payment_date"] if "dividend_payment_date" in frame else day
                payment = pd.Timestamp(payment).date() if pd.notna(payment) else day.date()
                receivable = p.declare_dividend(symbol, float(frame.loc[day, "dividend"]), day.date(), payment, cfg.execution, fx)
                if receivable:
                    connection.execute(
                        "INSERT OR IGNORE INTO dividend_receivables(symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,status,currency,net_native,gross_native,tax_native) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)",
                        (receivable.symbol, str(receivable.ex_date), str(receivable.payment_date),
                         receivable.net_inr, receivable.gross_inr, receivable.tax_inr,
                         receivable.currency, receivable.net_native, receivable.gross_native, receivable.tax_native),
                    )
        paid_now = p.pay_dividends(day.date(), fx)
        for event in paid_now:
            connection.execute(
                "UPDATE dividend_receivables SET status='PAID' WHERE symbol=? AND ex_date=? AND payment_date=? AND net_inr=?",
                (event.symbol, str(event.ex_date), str(event.payment_date), event.net_inr),
            )

        # Cash-settle only when an explicit, positive delisting consideration is supplied.
        # This is a paper accounting event, not a broker order.
        for symbol, frame in prices.items():
            if day not in frame.index or "delisting_price" not in frame.columns:
                continue
            settlement = frame.loc[day, "delisting_price"]
            if pd.isna(settlement):
                continue
            if float(settlement) <= 0:
                raise ValueError(f"Invalid delisting settlement for {symbol} on {day.date()}")
            connection.execute("UPDATE orders SET status='CANCELLED' WHERE symbol=? AND status='PENDING'", (symbol,))
            if symbol in p.positions:
                reason = "Cash delisting settlement"
                fill = p.sell(symbol, float(settlement), cfg.execution, day.date(), reason, usd_inr=fx, volume=None)
                if fill:
                    cursor = connection.execute(
                        "INSERT INTO orders(created_date,symbol,side,target_weight,reason,status,filled_date) VALUES(?,?, 'SELL',0,?,'FILLED',?)",
                        (str(day.date()), symbol, reason, str(day.date())),
                    )
                    connection.execute(
                        "INSERT INTO fills(order_id,date,symbol,side,quantity,price_native,fee_inr,reason) VALUES(?,?,?,?,?,?,?,?)",
                        (cursor.lastrowid, str(fill.date), fill.symbol, fill.side, fill.quantity,
                         fill.price_native, fill.fee_inr, fill.reason),
                    )

        # Execute prior-close orders only on the symbol's next actual session.
        for oid, symbol, side, target, reason in connection.execute(
            "SELECT id,symbol,side,target_weight,reason FROM orders WHERE status='PENDING'"
        ).fetchall():
            if symbol not in open_quotes:
                continue
            row = prices[symbol].loc[day]
            op = float(row["open"])
            volume = float(row["volume"]) if "volume" in row else None
            total = p.value(open_quotes, currencies, fx)
            if side == "SELL":
                position = p.positions.get(symbol)
                qty = quantity_to_target(position, op, p.fx(position.currency, fx), total, target) if position and target is not None else None
                fill = p.sell(symbol, op, cfg.execution, day.date(), reason, quantity=qty, usd_inr=fx, volume=volume)
            else:
                current = p.positions.get(symbol)
                current_value = current.quantity * op * p.fx(currencies[symbol], fx) if current else 0
                sector = str(metadata.loc[symbol].get("sector", "Unknown"))
                sector_value = sum(
                    pos.quantity * p.last_prices.get(sym, pos.avg_cost_native)
                    * p.fx(currencies.get(sym, pos.currency), fx)
                    for sym, pos in p.positions.items() if pos.sector == sector
                )
                budget = min(max(0, total * target - current_value),
                             max(0, total * cfg.risk.max_sector_weight - sector_value))
                fill = p.buy(symbol, op, currencies[symbol], sector, budget, cfg.execution, day.date(), reason, fx, volume=volume)
            if fill:
                connection.execute("UPDATE orders SET status='FILLED',filled_date=? WHERE id=?", (str(day.date()), oid))
                connection.execute(
                    "INSERT INTO fills(order_id,date,symbol,side,quantity,price_native,fee_inr,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (oid, str(fill.date), fill.symbol, fill.side, fill.quantity, fill.price_native, fill.fee_inr, fill.reason),
                )

        if day == dates[-1]:
            available = fundamentals_as_of(fundamentals, day)
            signals = dividend_quality(available) + dividend_growth(available) + long_term_growth(prices, day)
            targets = target_weights(signals, cfg.allocations, cfg.risk.max_position_weight)
            drift = drift_targets(p, close, currencies, fx, cfg.risk)

            def queue_order(symbol, side, target_weight, reason):
                connection.execute("UPDATE orders SET status='CANCELLED' WHERE symbol=? AND status='PENDING'", (symbol,))
                connection.execute(
                    "INSERT INTO orders(created_date,symbol,side,target_weight,reason,status) VALUES(?,?,?,?,?,'PENDING')",
                    (str(day.date()), symbol, side, target_weight, reason),
                )

            if not signals:
                connection.execute("UPDATE orders SET status='CANCELLED' WHERE status='PENDING' AND side='BUY'")
            else:
                for symbol in list(p.positions):
                    if symbol not in targets:
                        queue_order(symbol, "SELL", 0.0, "Strategy exit: no qualifying signal")
            for symbol, (weight, reason) in targets.items():
                if symbol in drift:
                    reduced_weight, drift_reason = drift[symbol]
                    queue_order(symbol, "SELL", reduced_weight, drift_reason)
                else:
                    queue_order(symbol, "BUY", weight, reason)
            if not signals:
                for symbol, (weight, reason) in drift.items():
                    queue_order(symbol, "SELL", weight, reason)
            connection.executemany(
                "INSERT INTO decisions(date,symbol,strategy,score,explanation) VALUES(?,?,?,?,?)",
                [(str(day.date()), x.symbol, x.strategy, x.score, x.explanation) for x in signals],
            )

        value = p.value(close, currencies, fx)
        insert_snapshot(connection, day.date(), p, value)
        connection.execute("INSERT INTO paper_sessions(date) VALUES(?)", (str(day.date()),))
    save_portfolio(connection, p, str(dates[-1].date()))
    connection.commit()
    return {"processed": len(dates), "portfolio": p}
