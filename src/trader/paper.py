"""Stateful, idempotent paper execution; never connects to a broker."""
from dataclasses import replace
import hashlib
import math
import pandas as pd
from .strategies import dividend_quality, dividend_growth, long_term_growth, target_weights, fundamentals_as_of
from .storage import (
    load_portfolio,
    save_portfolio,
    insert_snapshot,
    recover_interrupted_sessions,
    start_paper_session,
    complete_paper_session,
    fail_paper_session,
)
from .calendars import valid_sessions
from .risk import drift_targets, quantity_to_target


def run_paper(prices, fundamentals, cfg, connection, as_of=None, usd_inr=None, validate_calendars=True, corporate_actions=None):
    recover_interrupted_sessions(connection)

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

    def is_symbol_session_open(sym, day_ts):
        if calendar_days and sym in calendar_days:
            return day_ts in calendar_days[sym]
        if sym in prices:
            return day_ts in prices[sym].index
        return False

    config_hash = hashlib.sha256(repr(cfg).encode()).hexdigest()[:16]
    run_id = start_paper_session(
        connection,
        session_date=str(dates[-1].date()),
        config_hash=config_hash,
        starting_cash=p.cash_inr
    )

    orders_created = 0
    orders_filled = 0
    orders_cancelled = 0
    orders_expired = 0
    orders_rejected = 0
    final_value = p.cash_inr

    try:
        with connection:
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

                # Stock splits
                for symbol, frame in prices.items():
                    if day in frame.index:
                        p.apply_split(symbol, float(frame.loc[day, "split_factor"]) if "split_factor" in frame else 1)

                # Process structured corporate actions if provided
                if corporate_actions is not None and not corporate_actions.empty:
                    day_actions = corporate_actions[corporate_actions["date"] == day]
                    for _, ca in day_actions.iterrows():
                        act_type = str(ca["action_type"]).upper()
                        ca_sym = str(ca["symbol"])
                        if act_type == "STOCK_MERGER":
                            target_sym = str(ca.get("target_symbol", ""))
                            ratio = float(ca.get("ratio", 1.0))
                            if ca_sym in p.positions and target_sym:
                                p.apply_merger(ca_sym, target_sym, ratio, day.date(),
                                               acquirer_currency=currencies.get(target_sym, "INR"))
                        elif act_type == "WORTHLESS_LIQUIDATION":
                            if ca_sym in p.positions:
                                p.sell(ca_sym, 0.0, replace(cfg.execution, slippage_bps=0), day.date(), "Worthless liquidation", usd_inr=fx, volume=None)
                        elif act_type == "CASH_DELISTING":
                            cash_amt = float(ca.get("cash_amount", 0.0))
                            if ca_sym in p.positions:
                                p.sell(ca_sym, cash_amt, replace(cfg.execution, slippage_bps=0), day.date(), "Cash delisting", usd_inr=fx, volume=None)

                # Quarantine orphaned securities that permanently stopped trading without delisting
                for sym, pos in list(p.positions.items()):
                    if is_symbol_session_open(sym, day) and sym not in open_quotes:
                        future_dates = [d for d in (calendar_days[sym] if calendar_days and sym in calendar_days else prices.get(sym, pd.DataFrame()).index) if d >= day]
                        if not future_dates or all(d not in prices.get(sym, pd.DataFrame()).index for d in future_dates):
                            pos.is_quarantined = True

                # Settle due dividends
                paid = p.pay_dividends(day.date(), fx)
                for event in paid:
                    connection.execute(
                        "UPDATE dividend_receivables SET status='PAID', actual_paid_inr=?, paid_at=? "
                        "WHERE symbol=? AND ex_date=? AND payment_date=?",
                        (event.actual_paid_inr, str(day.date()), event.symbol, str(event.ex_date), str(event.payment_date)),
                    )

                # Determine ex-date entitlement from shares held coming into the session
                for symbol, frame in prices.items():
                    if day in frame.index and float(frame.loc[day, "dividend"]) > 0:
                        payment = frame.loc[day, "dividend_payment_date"] if "dividend_payment_date" in frame else day
                        payment = pd.Timestamp(payment).date() if pd.notna(payment) else day.date()
                        receivable = p.declare_dividend(symbol, float(frame.loc[day, "dividend"]), day.date(), payment, cfg.execution, fx)
                        if receivable:
                            connection.execute(
                                """INSERT INTO dividend_receivables(symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,status,currency,net_native,gross_native,tax_native)
                                   VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)
                                   ON CONFLICT(symbol, ex_date, payment_date) DO NOTHING""",
                                (receivable.symbol, str(receivable.ex_date), str(receivable.payment_date),
                                 receivable.net_inr, receivable.gross_inr, receivable.tax_inr,
                                 receivable.currency, receivable.net_native, receivable.gross_native, receivable.tax_native),
                            )

                paid_now = p.pay_dividends(day.date(), fx)
                for event in paid_now:
                    connection.execute(
                        "UPDATE dividend_receivables SET status='PAID', actual_paid_inr=?, paid_at=? "
                        "WHERE symbol=? AND ex_date=? AND payment_date=?",
                        (event.actual_paid_inr, str(day.date()), event.symbol, str(event.ex_date), str(event.payment_date)),
                    )

                # Delisting settlement (supports settlement >= 0.0 for bankruptcy write-offs)
                for symbol, frame in prices.items():
                    if day not in frame.index or "delisting_price" not in frame.columns:
                        continue
                    settlement = frame.loc[day, "delisting_price"]
                    if pd.isna(settlement):
                        continue
                    settlement_val = float(settlement)
                    if settlement_val < 0 or (settlement_val == 0 and not getattr(cfg.paper, "allow_zero_delisting", True)):
                        raise ValueError(f"Invalid delisting settlement for {symbol} on {day.date()}")

                    cur_cancel = connection.execute(
                        "UPDATE orders SET status='CANCELLED', cancellation_reason='Security delisted' WHERE symbol=? AND status='PENDING'",
                        (symbol,)
                    )
                    orders_cancelled += cur_cancel.rowcount

                    if symbol in p.positions:
                        reason = "Cash delisting settlement"
                        fill = p.sell(symbol, settlement_val, replace(cfg.execution, slippage_bps=0), day.date(), reason, usd_inr=fx, volume=None, tax_cfg=getattr(cfg, "tax", None))
                        if fill:
                            cursor = connection.execute(
                                """INSERT INTO orders(run_id, created_date, symbol, side, order_type, target_weight, requested_quantity, filled_quantity, execution_price, reason, status, filled_date)
                                   VALUES(?, ?, ?, 'SELL', 'MARKET', 0, ?, ?, ?, ?, 'FILLED', ?)""",
                                (run_id, str(day.date()), symbol, fill.quantity, fill.quantity, fill.price_native, reason, str(day.date()))
                            )
                            orders_created += 1
                            orders_filled += 1
                            connection.execute(
                                "INSERT INTO fills(order_id,date,symbol,side,quantity,price_native,fee_inr,reason) VALUES(?,?,?,?,?,?,?,?)",
                                (cursor.lastrowid, str(fill.date), fill.symbol, fill.side, fill.quantity,
                                 fill.price_native, fill.fee_inr, fill.reason),
                            )

                # Execute prior-close orders only on the symbol's next actual session
                pending_orders = connection.execute(
                    """SELECT id, symbol, side, target_weight, requested_quantity, filled_quantity,
                              order_type, time_in_force, reason, created_date, limit_price
                       FROM orders WHERE status='PENDING'"""
                ).fetchall()

                for oid, symbol, side, target, req_qty, filled_qty, ord_type, tif, reason, created_date, limit_px in pending_orders:
                    if pd.Timestamp(created_date).date() >= day.date():
                        continue

                    if not is_symbol_session_open(symbol, day):
                        continue

                    if symbol not in open_quotes:
                        if tif == "DAY":
                            connection.execute(
                                "UPDATE orders SET status='EXPIRED', cancellation_reason='Expired: untradable or no quotes on session' WHERE id=?",
                                (oid,)
                            )
                            orders_expired += 1
                        elif tif == "GTC":
                            created_dt = pd.Timestamp(created_date)
                            sessions_list = calendar_days[symbol] if (calendar_days and symbol in calendar_days) else prices[symbol].index
                            sessions_since = [s for s in sessions_list if created_dt < s <= day]
                            if len(sessions_since) >= getattr(cfg.paper, "gtc_expiration_trading_days", 5):
                                connection.execute(
                                    "UPDATE orders SET status='EXPIRED', cancellation_reason='Expired: GTC trading day limit reached' WHERE id=?",
                                    (oid,)
                                )
                                orders_expired += 1
                        continue

                    row = prices[symbol].loc[day]
                    op = float(row["open"])
                    low_px = float(row["low"]) if "low" in row else op
                    high_px = float(row["high"]) if "high" in row else op
                    volume = float(row["volume"]) if "volume" in row else None
                    total = p.value(open_quotes, currencies, fx)
                    fill = None

                    # Limit price checks
                    if ord_type == "LIMIT" and limit_px is not None:
                        if side == "BUY" and low_px > limit_px:
                            continue
                        if side == "SELL" and high_px < limit_px:
                            continue
                        exec_px = min(op, float(limit_px)) if side == "BUY" else max(op, float(limit_px))
                    else:
                        exec_px = op

                    # Volatility / volume-scaled slippage
                    exec_cfg = cfg.execution
                    if getattr(exec_cfg, "use_volatility_slippage", False) and volume and volume > 0:
                        est_shares = req_qty or 100
                        vol_ratio = math.sqrt(min(1.0, est_shares / volume))
                        scaled_slip = exec_cfg.slippage_bps * (1.0 + vol_ratio)
                        exec_cfg = replace(exec_cfg, slippage_bps=scaled_slip)

                    tax_cfg = getattr(cfg, "tax", None)

                    if side == "SELL":
                        position = p.positions.get(symbol)
                        if not position:
                            connection.execute(
                                "UPDATE orders SET status='REJECTED', rejection_reason='No position held to sell' WHERE id=?",
                                (oid,)
                            )
                            orders_rejected += 1
                            continue
                        qty = quantity_to_target(position, exec_px, p.fx(position.currency, fx), total, target) if position and target is not None else (req_qty - filled_qty if req_qty else None)
                        if qty is not None and qty <= 0:
                            connection.execute(
                                "UPDATE orders SET status='CANCELLED', cancellation_reason='Target position already reached' WHERE id=?",
                                (oid,)
                            )
                            orders_cancelled += 1
                            continue

                        max_qty = math.floor(max(0.0, float(volume)) * exec_cfg.max_volume_participation) if volume is not None else None
                        if max_qty is not None and max_qty < 1:
                            fill = None
                        elif max_qty is not None and qty is not None and qty > max_qty:
                            if getattr(cfg.paper, "allow_partial_fills", False) and max_qty > 0:
                                fill = p.sell(symbol, exec_px, exec_cfg, day.date(), reason, quantity=max_qty, usd_inr=fx, volume=None, tax_cfg=tax_cfg)
                            else:
                                fill = None
                        else:
                            fill = p.sell(symbol, exec_px, exec_cfg, day.date(), reason, quantity=qty, usd_inr=fx, volume=None, tax_cfg=tax_cfg)

                    else: # BUY
                        current = p.positions.get(symbol)
                        current_value = current.quantity * exec_px * p.fx(currencies[symbol], fx) if current else 0
                        sector = str(metadata.loc[symbol].get("sector", "Unknown"))
                        sector_value = sum(
                            pos.quantity * p.last_prices.get(sym, pos.avg_cost_native)
                            * p.fx(currencies.get(sym, pos.currency), fx)
                            for sym, pos in p.positions.items() if pos.sector == sector
                        )
                        budget = min(max(0, total * target - current_value),
                                     max(0, total * cfg.risk.max_sector_weight - sector_value)) if target is not None else 0
                        if budget <= 0:
                            fill = None
                        else:
                            fx_rate = p.fx(currencies[symbol], fx)
                            px = exec_px * (1 + exec_cfg.slippage_bps / 10000)
                            rate = (exec_cfg.us_cost_bps if currencies[symbol].upper() == "USD" else exec_cfg.india_cost_bps) / 10000
                            cost_per_share = px * fx_rate * (1 + rate)
                            needed_shares = math.floor(budget / cost_per_share) if cost_per_share > 0 else 0
                            if needed_shares < 1:
                                fill = None
                            else:
                                max_qty = math.floor(max(0.0, float(volume)) * exec_cfg.max_volume_participation) if volume is not None else None
                                if max_qty is not None and max_qty < 1:
                                    fill = None
                                elif max_qty is not None and needed_shares > max_qty:
                                    if getattr(cfg.paper, "allow_partial_fills", False) and max_qty > 0:
                                        partial_budget = max_qty * cost_per_share
                                        fill = p.buy(symbol, exec_px, currencies[symbol], sector, partial_budget, exec_cfg, day.date(), reason, fx, volume=None)
                                    else:
                                        fill = None
                                else:
                                    fill = p.buy(symbol, exec_px, currencies[symbol], sector, budget, exec_cfg, day.date(), reason, fx, volume=None)

                    if fill:
                        new_filled_qty = (filled_qty or 0) + fill.quantity
                        connection.execute(
                            "INSERT INTO fills(order_id,date,symbol,side,quantity,price_native,fee_inr,reason) VALUES(?,?,?,?,?,?,?,?)",
                            (oid, str(fill.date), fill.symbol, fill.side, fill.quantity, fill.price_native, fill.fee_inr, fill.reason),
                        )
                        is_full = True
                        if getattr(cfg.paper, "allow_partial_fills", False) and req_qty and new_filled_qty < req_qty:
                            is_full = False

                        if is_full:
                            connection.execute(
                                "UPDATE orders SET status='FILLED', filled_date=?, filled_quantity=?, execution_price=?, fee_inr=fee_inr+? WHERE id=?",
                                (str(day.date()), new_filled_qty, fill.price_native, fill.fee_inr, oid)
                            )
                            orders_filled += 1
                        else:
                            if tif == "DAY":
                                connection.execute(
                                    "UPDATE orders SET status='EXPIRED', filled_date=?, filled_quantity=?, execution_price=?, fee_inr=fee_inr+?, "
                                    "cancellation_reason='Partially filled: unfilled remainder expired at end of DAY session' WHERE id=?",
                                    (str(day.date()), new_filled_qty, fill.price_native, fill.fee_inr, oid)
                                )
                                orders_expired += 1
                            else:
                                connection.execute(
                                    "UPDATE orders SET filled_date=?, filled_quantity=?, execution_price=?, fee_inr=fee_inr+? WHERE id=?",
                                    (str(day.date()), new_filled_qty, fill.price_native, fill.fee_inr, oid)
                                )
                    else:
                        if tif == "DAY":
                            connection.execute(
                                "UPDATE orders SET status='EXPIRED', cancellation_reason='Expired: end of DAY trading session reached without full execution' WHERE id=?",
                                (oid,)
                            )
                            orders_expired += 1
                        elif tif == "GTC":
                            created_dt = pd.Timestamp(created_date)
                            sessions_list = calendar_days[symbol] if (calendar_days and symbol in calendar_days) else prices[symbol].index
                            sessions_since = [s for s in sessions_list if created_dt < s <= day]
                            if len(sessions_since) >= getattr(cfg.paper, "gtc_expiration_trading_days", 5):
                                connection.execute(
                                    "UPDATE orders SET status='EXPIRED', cancellation_reason='Expired: GTC trading day limit reached' WHERE id=?",
                                    (oid,)
                                )
                                orders_expired += 1

                # Rebalancing signals and target weights queued only at batch end
                if day == dates[-1]:
                    available = fundamentals_as_of(fundamentals, day)
                    signals = dividend_quality(available) + dividend_growth(available) + long_term_growth(prices, day)
                    targets = target_weights(signals, cfg.allocations, cfg.risk.max_position_weight)
                    drift = drift_targets(p, close, currencies, fx, cfg.risk)

                    def queue_order(symbol, side, target_weight, reason):
                        nonlocal orders_created, orders_cancelled
                        existing = connection.execute(
                            "SELECT id, side, target_weight FROM orders WHERE symbol=? AND status='PENDING'",
                            (symbol,)
                        ).fetchone()
                        if existing:
                            ex_id, ex_side, ex_target = existing
                            if ex_side == side and abs((ex_target or 0.0) - target_weight) < 1e-6:
                                return
                            cur = connection.execute(
                                "UPDATE orders SET status='CANCELLED', cancellation_reason='Superseded by updated rebalancing order' WHERE id=?",
                                (ex_id,)
                            )
                            orders_cancelled += cur.rowcount

                        ref_px = close.get(symbol, open_quotes.get(symbol, 0.0))
                        total_equity = p.value(close, currencies, fx)
                        sec_fx = p.fx(currencies.get(symbol, "INR"), fx)
                        cost_unit = ref_px * sec_fx if ref_px > 0 else 1.0
                        target_val = total_equity * target_weight
                        req_qty = math.floor(target_val / cost_unit) if side == "BUY" and cost_unit > 0 else (p.positions[symbol].quantity if symbol in p.positions and side == "SELL" and target_weight == 0 else None)
                        connection.execute(
                            """INSERT INTO orders(run_id, created_date, symbol, side, order_type, target_weight,
                                                 requested_quantity, reference_price, time_in_force, reason, status)
                               VALUES(?, ?, ?, ?, 'MARKET', ?, ?, ?, ?, ?, 'PENDING')""",
                            (run_id, str(day.date()), symbol, side, target_weight,
                             req_qty, ref_px, cfg.paper.default_time_in_force, reason)
                        )
                        orders_created += 1

                    # Zombie order cancellation: cancel pending BUY orders for symbols no longer in strategy targets
                    pending_buys = connection.execute(
                        "SELECT symbol FROM orders WHERE status='PENDING' AND side='BUY'"
                    ).fetchall()
                    for (pb_sym,) in pending_buys:
                        if pb_sym not in targets:
                            cur = connection.execute(
                                "UPDATE orders SET status='CANCELLED', cancellation_reason='Disqualified: symbol no longer in strategy targets' WHERE status='PENDING' AND side='BUY' AND symbol=?",
                                (pb_sym,)
                            )
                            orders_cancelled += cur.rowcount

                    if not signals:
                        cur = connection.execute("UPDATE orders SET status='CANCELLED', cancellation_reason='No qualifying signals in strategy' WHERE status='PENDING' AND side='BUY'")
                        orders_cancelled += cur.rowcount
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

                final_value = p.value(close, currencies, fx)
                insert_snapshot(connection, day.date(), p, final_value)

            save_portfolio(connection, p, str(dates[-1].date()))
    except Exception as exc:
        fail_paper_session(connection, run_id, str(exc))
        raise exc

    complete_paper_session(
        connection,
        run_id=run_id,
        ending_cash=p.cash_inr,
        portfolio_value=final_value,
        realised_pnl=p.realised_pnl_inr,
        dividend_income=p.dividend_income_inr,
        orders_created=orders_created,
        orders_filled=orders_filled,
        orders_cancelled=orders_cancelled,
        orders_expired=orders_expired,
        orders_rejected=orders_rejected,
    )

    return {"processed": len(dates), "portfolio": p, "run_id": run_id}
