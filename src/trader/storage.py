import sqlite3
from pathlib import Path
from datetime import date
from .portfolio import Portfolio, Position, DividendReceivable

SCHEMA='''
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, order_id INTEGER, date TEXT, symbol TEXT, side TEXT, quantity INTEGER, price_native REAL, fee_inr REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS snapshots (date TEXT PRIMARY KEY, value_inr REAL, cash_inr REAL, realised_pnl_inr REAL, dividend_income_inr REAL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, strategy TEXT, score REAL, explanation TEXT);
CREATE TABLE IF NOT EXISTS portfolio_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), cash_inr REAL NOT NULL, realised_pnl_inr REAL NOT NULL, dividend_income_inr REAL NOT NULL, last_processed_date TEXT);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, avg_cost_native REAL NOT NULL, avg_cost_inr REAL NOT NULL, currency TEXT NOT NULL, sector TEXT NOT NULL, last_price_native REAL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, created_date TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, target_weight REAL, reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','FILLED','CANCELLED')), filled_date TEXT);
CREATE TABLE IF NOT EXISTS dividend_receivables (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, ex_date TEXT NOT NULL, payment_date TEXT NOT NULL, net_inr REAL NOT NULL, gross_inr REAL NOT NULL, tax_inr REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','PAID')), currency TEXT NOT NULL DEFAULT 'INR', net_native REAL, gross_native REAL, tax_native REAL, UNIQUE(symbol,ex_date,payment_date,gross_inr));
CREATE TABLE IF NOT EXISTS paper_sessions (date TEXT PRIMARY KEY, completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
'''
def connect(path="data/trader.db"):
    Path(path).parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(path); c.executescript(SCHEMA)
    # Lightweight forward migration for databases created by the first Phase 1
    # schema, which did not associate fills with a paper order.
    columns={row[1] for row in c.execute("PRAGMA table_info(fills)")}
    if "order_id" not in columns: c.execute("ALTER TABLE fills ADD COLUMN order_id INTEGER")
    dividend_columns={row[1] for row in c.execute("PRAGMA table_info(dividend_receivables)")}
    if "ex_date" not in dividend_columns:
        c.execute("ALTER TABLE dividend_receivables RENAME TO dividend_receivables_legacy")
        c.execute("CREATE TABLE dividend_receivables (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, ex_date TEXT NOT NULL, payment_date TEXT NOT NULL, net_inr REAL NOT NULL, gross_inr REAL NOT NULL, tax_inr REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','PAID')), UNIQUE(symbol,ex_date,payment_date,gross_inr))")
        c.execute("INSERT INTO dividend_receivables(symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,status) SELECT symbol,'',payment_date,net_inr,gross_inr,tax_inr,status FROM dividend_receivables_legacy")
        c.execute("DROP TABLE dividend_receivables_legacy")
    receivable_columns={row[1] for row in c.execute("PRAGMA table_info(dividend_receivables)")}
    for name, declaration in (("currency", "TEXT NOT NULL DEFAULT 'INR'"), ("net_native", "REAL"), ("gross_native", "REAL"), ("tax_native", "REAL")):
        if name not in receivable_columns:
            c.execute(f"ALTER TABLE dividend_receivables ADD COLUMN {name} {declaration}")
    # Existing rows were stored as INR values, so migrate them as INR-native.
    c.execute("UPDATE dividend_receivables SET net_native=net_inr WHERE net_native IS NULL")
    c.execute("UPDATE dividend_receivables SET gross_native=gross_inr WHERE gross_native IS NULL")
    c.execute("UPDATE dividend_receivables SET tax_native=tax_inr WHERE tax_native IS NULL")
    return c

def load_portfolio(connection, initial_capital: float) -> tuple[Portfolio, str | None]:
    row=connection.execute("SELECT * FROM portfolio_state WHERE singleton=1").fetchone()
    if row is None: return Portfolio(initial_capital), None
    p=Portfolio(row[1], realised_pnl_inr=row[2], dividend_income_inr=row[3])
    for symbol,qty,avg_native,avg_inr,currency,sector,last_price in connection.execute("SELECT symbol,quantity,avg_cost_native,avg_cost_inr,currency,sector,last_price_native FROM positions"):
        p.positions[symbol]=Position(symbol,qty,avg_native,avg_inr,currency,sector)
        if last_price is not None: p.last_prices[symbol]=last_price
    for symbol,ex,payment,net,gross,tax,currency,net_native,gross_native,tax_native in connection.execute("SELECT symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,currency,net_native,gross_native,tax_native FROM dividend_receivables WHERE status='PENDING'"):
        p.dividend_receivables.append(DividendReceivable(symbol,date.fromisoformat(ex),date.fromisoformat(payment),net,gross,tax,currency,net_native,gross_native,tax_native))
    return p,row[4]

def save_portfolio(connection, portfolio: Portfolio, last_processed_date: str) -> None:
    connection.execute("INSERT OR REPLACE INTO portfolio_state VALUES(1,?,?,?,?)",(portfolio.cash_inr,portfolio.realised_pnl_inr,portfolio.dividend_income_inr,last_processed_date))
    connection.execute("DELETE FROM positions")
    connection.executemany("INSERT INTO positions VALUES(?,?,?,?,?,?,?)",[(x.symbol,x.quantity,x.avg_cost_native,x.avg_cost_inr,x.currency,x.sector,portfolio.last_prices.get(x.symbol)) for x in portfolio.positions.values()])
    connection.execute("DELETE FROM dividend_receivables WHERE status='PENDING'")
    connection.executemany("INSERT INTO dividend_receivables(symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,status,currency,net_native,gross_native,tax_native) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)",[(x.symbol,str(x.ex_date),str(x.payment_date),x.net_inr,x.gross_inr,x.tax_inr,x.currency,x.net_native,x.gross_native,x.tax_native) for x in portfolio.dividend_receivables])

def insert_snapshot(connection, day, portfolio: Portfolio, value: float) -> None:
    connection.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",(str(day),value,portfolio.cash_inr,portfolio.realised_pnl_inr,portfolio.dividend_income_inr))
def persist_result(result, connection):
    connection.executemany("INSERT INTO fills(order_id,date,symbol,side,quantity,price_native,fee_inr,reason) VALUES(NULL,?,?,?,?,?,?,?)",[(str(x.date),x.symbol,x.side,x.quantity,x.price_native,x.fee_inr,x.reason) for x in result.portfolio.fills])
    connection.executemany("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",[(str(day),value,cash,realised,dividend) for day,value,cash,realised,dividend in result.snapshots])
    connection.executemany("INSERT INTO decisions(date,symbol,strategy,score,explanation) VALUES(?,?,?,?,?)",[("",x.symbol,x.strategy,x.score,x.explanation) for x in result.decisions]); connection.commit()
