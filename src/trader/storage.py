import sqlite3
import uuid
from pathlib import Path
from datetime import date
from contextlib import contextmanager
from .portfolio import Portfolio, Position, DividendReceivable

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"
ORDER_STATUS_EXPIRED = "EXPIRED"
ORDER_STATUS_REJECTED = "REJECTED"
VALID_ORDER_STATUSES = {
    ORDER_STATUS_PENDING,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_EXPIRED,
    ORDER_STATUS_REJECTED,
}

SESSION_STATUS_RUNNING = "RUNNING"
SESSION_STATUS_SUCCESS = "SUCCESS"
SESSION_STATUS_FAILED = "FAILED"

SCHEMA = '''
CREATE TABLE IF NOT EXISTS paper_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    session_date TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    config_hash TEXT,
    starting_cash_inr REAL,
    ending_cash_inr REAL,
    portfolio_value_inr REAL,
    realised_pnl_inr REAL,
    dividend_income_inr REAL,
    orders_created INTEGER DEFAULT 0,
    orders_filled INTEGER DEFAULT 0,
    orders_cancelled INTEGER DEFAULT 0,
    orders_expired INTEGER DEFAULT 0,
    orders_rejected INTEGER DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    created_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL DEFAULT 'MARKET' CHECK(order_type IN ('MARKET', 'LIMIT')),
    target_weight REAL,
    requested_quantity INTEGER,
    filled_quantity INTEGER DEFAULT 0,
    limit_price REAL,
    reference_price REAL,
    execution_price REAL,
    fee_inr REAL DEFAULT 0.0,
    time_in_force TEXT NOT NULL DEFAULT 'DAY' CHECK(time_in_force IN ('DAY', 'GTC')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED')),
    cancellation_reason TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    filled_date TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES paper_sessions(run_id)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL,
    price_native REAL NOT NULL,
    fee_inr REAL NOT NULL,
    reason TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    date TEXT PRIMARY KEY,
    value_inr REAL,
    cash_inr REAL,
    realised_pnl_inr REAL,
    dividend_income_inr REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    symbol TEXT,
    strategy TEXT,
    score REAL,
    explanation TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    cash_inr REAL NOT NULL,
    realised_pnl_inr REAL NOT NULL,
    dividend_income_inr REAL NOT NULL,
    last_processed_date TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    avg_cost_native REAL NOT NULL,
    avg_cost_inr REAL NOT NULL,
    currency TEXT NOT NULL,
    sector TEXT NOT NULL,
    last_price_native REAL
);
CREATE TABLE IF NOT EXISTS dividend_receivables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    payment_date TEXT NOT NULL,
    net_inr REAL NOT NULL,
    gross_inr REAL NOT NULL,
    tax_inr REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PAID')),
    currency TEXT NOT NULL DEFAULT 'INR',
    net_native REAL,
    gross_native REAL,
    tax_native REAL,
    actual_paid_inr REAL,
    paid_at TEXT,
    UNIQUE(symbol, ex_date, payment_date)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders(symbol, status);
CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_symbol_date ON fills(symbol, date);
CREATE INDEX IF NOT EXISTS idx_dividends_status_paydate ON dividend_receivables(status, payment_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(date);
CREATE INDEX IF NOT EXISTS idx_paper_sessions_date ON paper_sessions(session_date);
'''


def migrate_db(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    current_version = version_row[0] if version_row else 0
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if not tables:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        return

    if current_version < 2:
        connection.execute("PRAGMA foreign_keys = OFF;")
        # Check fills table
        if "fills" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(fills)")}
            if "order_id" not in columns:
                connection.execute("ALTER TABLE fills ADD COLUMN order_id INTEGER")
        else:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    quantity INTEGER NOT NULL,
                    price_native REAL NOT NULL,
                    fee_inr REAL NOT NULL,
                    reason TEXT,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                )
            """)

        # Check dividend_receivables table
        if "dividend_receivables" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(dividend_receivables)")}
            if "ex_date" not in columns:
                connection.execute("ALTER TABLE dividend_receivables RENAME TO dividend_receivables_legacy")
                connection.execute("""
                    CREATE TABLE dividend_receivables (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        ex_date TEXT NOT NULL,
                        payment_date TEXT NOT NULL,
                        net_inr REAL NOT NULL,
                        gross_inr REAL NOT NULL,
                        tax_inr REAL NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('PENDING','PAID')),
                        UNIQUE(symbol,ex_date,payment_date,gross_inr)
                    )
                """)
                connection.execute("""
                    INSERT INTO dividend_receivables(symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,status)
                    SELECT symbol,payment_date,payment_date,net_inr,gross_inr,tax_inr,status FROM dividend_receivables_legacy
                """)
                connection.execute("DROP TABLE dividend_receivables_legacy")

            columns = {row[1] for row in connection.execute("PRAGMA table_info(dividend_receivables)")}
            for col, decl in [
                ("currency", "TEXT NOT NULL DEFAULT 'INR'"),
                ("net_native", "REAL"),
                ("gross_native", "REAL"),
                ("tax_native", "REAL"),
                ("actual_paid_inr", "REAL"),
                ("paid_at", "TEXT")
            ]:
                if col not in columns:
                    connection.execute(f"ALTER TABLE dividend_receivables ADD COLUMN {col} {decl}")
            connection.execute("UPDATE dividend_receivables SET net_native=net_inr WHERE net_native IS NULL")
            connection.execute("UPDATE dividend_receivables SET gross_native=gross_inr WHERE gross_native IS NULL")
            connection.execute("UPDATE dividend_receivables SET tax_native=tax_inr WHERE tax_native IS NULL")

            # Upgrade unique constraint to (symbol, ex_date, payment_date)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dividend_receivables_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    ex_date TEXT NOT NULL,
                    payment_date TEXT NOT NULL,
                    net_inr REAL NOT NULL,
                    gross_inr REAL NOT NULL,
                    tax_inr REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PAID')),
                    currency TEXT NOT NULL DEFAULT 'INR',
                    net_native REAL,
                    gross_native REAL,
                    tax_native REAL,
                    actual_paid_inr REAL,
                    paid_at TEXT,
                    UNIQUE(symbol, ex_date, payment_date)
                )
            """)
            connection.execute("""
                INSERT OR IGNORE INTO dividend_receivables_v2(id, symbol, ex_date, payment_date, net_inr, gross_inr, tax_inr, status, currency, net_native, gross_native, tax_native, actual_paid_inr, paid_at)
                SELECT id, symbol, ex_date, payment_date, net_inr, gross_inr, tax_inr, status, currency, net_native, gross_native, tax_native, actual_paid_inr, paid_at
                FROM dividend_receivables
                WHERE id IN (
                    SELECT MIN(id) FROM dividend_receivables GROUP BY symbol, ex_date, payment_date
                )
            """)
            connection.execute("DROP TABLE dividend_receivables")
            connection.execute("ALTER TABLE dividend_receivables_v2 RENAME TO dividend_receivables")
        else:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dividend_receivables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    ex_date TEXT NOT NULL,
                    payment_date TEXT NOT NULL,
                    net_inr REAL NOT NULL,
                    gross_inr REAL NOT NULL,
                    tax_inr REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PAID')),
                    currency TEXT NOT NULL DEFAULT 'INR',
                    net_native REAL,
                    gross_native REAL,
                    tax_native REAL,
                    actual_paid_inr REAL,
                    paid_at TEXT,
                    UNIQUE(symbol, ex_date, payment_date)
                )
            """)

        # Check orders table
        if "orders" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
            if "order_type" not in columns or "time_in_force" not in columns:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS orders_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        created_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                        order_type TEXT NOT NULL DEFAULT 'MARKET' CHECK(order_type IN ('MARKET', 'LIMIT')),
                        target_weight REAL,
                        requested_quantity INTEGER,
                        filled_quantity INTEGER DEFAULT 0,
                        limit_price REAL,
                        reference_price REAL,
                        execution_price REAL,
                        fee_inr REAL DEFAULT 0.0,
                        time_in_force TEXT NOT NULL DEFAULT 'DAY' CHECK(time_in_force IN ('DAY', 'GTC')),
                        reason TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('PENDING', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED')),
                        cancellation_reason TEXT,
                        rejection_reason TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        filled_date TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(run_id) REFERENCES paper_sessions(run_id)
                    )
                """)
                connection.execute("""
                    INSERT INTO orders_v2(id, created_date, symbol, side, target_weight, reason, status, filled_date)
                    SELECT id, created_date, symbol, side, target_weight, reason, status, filled_date FROM orders
                """)
                connection.execute("DROP TABLE orders")
                connection.execute("ALTER TABLE orders_v2 RENAME TO orders")
        else:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    created_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    order_type TEXT NOT NULL DEFAULT 'MARKET' CHECK(order_type IN ('MARKET', 'LIMIT')),
                    target_weight REAL,
                    requested_quantity INTEGER,
                    filled_quantity INTEGER DEFAULT 0,
                    limit_price REAL,
                    reference_price REAL,
                    execution_price REAL,
                    fee_inr REAL DEFAULT 0.0,
                    time_in_force TEXT NOT NULL DEFAULT 'DAY' CHECK(time_in_force IN ('DAY', 'GTC')),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED')),
                    cancellation_reason TEXT,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    filled_date TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(run_id) REFERENCES paper_sessions(run_id)
                )
            """)

        # Check paper_sessions table
        if "paper_sessions" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_sessions)")}
            if "run_id" not in columns:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS paper_sessions_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL UNIQUE,
                        session_date TEXT NOT NULL,
                        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        completed_at TEXT,
                        config_hash TEXT,
                        starting_cash_inr REAL,
                        ending_cash_inr REAL,
                        portfolio_value_inr REAL,
                        realised_pnl_inr REAL,
                        dividend_income_inr REAL,
                        orders_created INTEGER DEFAULT 0,
                        orders_filled INTEGER DEFAULT 0,
                        orders_cancelled INTEGER DEFAULT 0,
                        orders_expired INTEGER DEFAULT 0,
                        orders_rejected INTEGER DEFAULT 0,
                        status TEXT NOT NULL CHECK(status IN ('RUNNING', 'SUCCESS', 'FAILED')),
                        error_message TEXT
                    )
                """)
                connection.execute("""
                    INSERT INTO paper_sessions_v2(run_id, session_date, started_at, completed_at, status)
                    SELECT 'legacy-' || date, date, completed_at, completed_at, 'SUCCESS' FROM paper_sessions
                """)
                connection.execute("DROP TABLE paper_sessions")
                connection.execute("ALTER TABLE paper_sessions_v2 RENAME TO paper_sessions")
        else:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS paper_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    session_date TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    config_hash TEXT,
                    starting_cash_inr REAL,
                    ending_cash_inr REAL,
                    portfolio_value_inr REAL,
                    realised_pnl_inr REAL,
                    dividend_income_inr REAL,
                    orders_created INTEGER DEFAULT 0,
                    orders_filled INTEGER DEFAULT 0,
                    orders_cancelled INTEGER DEFAULT 0,
                    orders_expired INTEGER DEFAULT 0,
                    orders_rejected INTEGER DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'SUCCESS', 'FAILED')),
                    error_message TEXT
                )
            """)

        connection.execute("CREATE TABLE IF NOT EXISTS snapshots (date TEXT PRIMARY KEY, value_inr REAL, cash_inr REAL, realised_pnl_inr REAL, dividend_income_inr REAL);")
        connection.execute("CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, symbol TEXT, strategy TEXT, score REAL, explanation TEXT);")
        connection.execute("CREATE TABLE IF NOT EXISTS portfolio_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), cash_inr REAL NOT NULL, realised_pnl_inr REAL NOT NULL, dividend_income_inr REAL NOT NULL, last_processed_date TEXT);")
        connection.execute("CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, avg_cost_native REAL NOT NULL, avg_cost_inr REAL NOT NULL, currency TEXT NOT NULL, sector TEXT NOT NULL, last_price_native REAL);")

        connection.execute("PRAGMA user_version = 2")
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.commit()

    connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders(symbol, status);")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_fills_symbol_date ON fills(symbol, date);")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_dividends_status_paydate ON dividend_receivables(status, payment_date);")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(date);")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_paper_sessions_date ON paper_sessions(session_date);")
    connection.commit()


def connect(path: str = "data/trader.db") -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON;")
    c.execute("PRAGMA journal_mode = WAL;")
    c.execute("PRAGMA busy_timeout = 5000;")
    migrate_db(c)
    return c


@contextmanager
def open_db(path: str = "data/trader.db"):
    c = connect(path)
    try:
        yield c
    finally:
        c.close()


def recover_interrupted_sessions(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "UPDATE paper_sessions SET status='FAILED', completed_at=CURRENT_TIMESTAMP, "
        "error_message='Interrupted: process terminated unexpectedly during session execution' "
        "WHERE status='RUNNING'"
    )
    connection.commit()
    return cursor.rowcount


def start_paper_session(
    connection: sqlite3.Connection,
    session_date: str,
    config_hash: str | None = None,
    starting_cash: float | None = None
) -> str:
    run_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO paper_sessions(run_id, session_date, started_at, config_hash, starting_cash_inr, status) "
        "VALUES(?, ?, CURRENT_TIMESTAMP, ?, ?, 'RUNNING')",
        (run_id, session_date, config_hash, starting_cash)
    )
    connection.commit()
    return run_id


def complete_paper_session(
    connection: sqlite3.Connection,
    run_id: str,
    ending_cash: float,
    portfolio_value: float,
    realised_pnl: float,
    dividend_income: float,
    orders_created: int = 0,
    orders_filled: int = 0,
    orders_cancelled: int = 0,
    orders_expired: int = 0,
    orders_rejected: int = 0,
) -> None:
    connection.execute(
        """UPDATE paper_sessions SET
            status='SUCCESS',
            completed_at=CURRENT_TIMESTAMP,
            ending_cash_inr=?,
            portfolio_value_inr=?,
            realised_pnl_inr=?,
            dividend_income_inr=?,
            orders_created=?,
            orders_filled=?,
            orders_cancelled=?,
            orders_expired=?,
            orders_rejected=?
        WHERE run_id=?""",
        (
            ending_cash, portfolio_value, realised_pnl, dividend_income,
            orders_created, orders_filled, orders_cancelled, orders_expired, orders_rejected,
            run_id
        )
    )
    connection.commit()


def fail_paper_session(connection: sqlite3.Connection, run_id: str, error_message: str) -> None:
    try:
        connection.execute(
            "UPDATE paper_sessions SET status='FAILED', completed_at=CURRENT_TIMESTAMP, error_message=? WHERE run_id=?",
            (error_message, run_id)
        )
        connection.commit()
    except Exception:
        pass


def load_portfolio(connection: sqlite3.Connection, initial_capital: float) -> tuple[Portfolio, str | None]:
    row = connection.execute("SELECT * FROM portfolio_state WHERE singleton=1").fetchone()
    if row is None:
        return Portfolio(initial_capital), None
    p = Portfolio(row[1], realised_pnl_inr=row[2], dividend_income_inr=row[3])
    for symbol, qty, avg_native, avg_inr, currency, sector, last_price in connection.execute(
        "SELECT symbol,quantity,avg_cost_native,avg_cost_inr,currency,sector,last_price_native FROM positions"
    ):
        p.positions[symbol] = Position(symbol, qty, avg_native, avg_inr, currency, sector)
        if last_price is not None:
            p.last_prices[symbol] = last_price
    for symbol, ex, payment, net, gross, tax, currency, net_native, gross_native, tax_native in connection.execute(
        "SELECT symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,currency,net_native,gross_native,tax_native "
        "FROM dividend_receivables WHERE status='PENDING'"
    ):
        p.dividend_receivables.append(
            DividendReceivable(
                symbol, date.fromisoformat(ex), date.fromisoformat(payment),
                net, gross, tax, currency, net_native, gross_native, tax_native
            )
        )
    return p, row[4]


def save_portfolio(connection: sqlite3.Connection, portfolio: Portfolio, last_processed_date: str) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO portfolio_state VALUES(1,?,?,?,?)",
        (portfolio.cash_inr, portfolio.realised_pnl_inr, portfolio.dividend_income_inr, last_processed_date)
    )
    for x in portfolio.positions.values():
        connection.execute(
            "INSERT OR REPLACE INTO positions(symbol, quantity, avg_cost_native, avg_cost_inr, currency, sector, last_price_native) "
            "VALUES(?,?,?,?,?,?,?)",
            (x.symbol, x.quantity, x.avg_cost_native, x.avg_cost_inr, x.currency, x.sector, portfolio.last_prices.get(x.symbol))
        )
    current_symbols = list(portfolio.positions.keys())
    if current_symbols:
        placeholders = ",".join("?" * len(current_symbols))
        connection.execute(f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})", current_symbols)
    else:
        connection.execute("DELETE FROM positions")

    for x in portfolio.dividend_receivables:
        connection.execute(
            """INSERT INTO dividend_receivables(symbol,ex_date,payment_date,net_inr,gross_inr,tax_inr,status,currency,net_native,gross_native,tax_native)
               VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)
               ON CONFLICT(symbol, ex_date, payment_date) DO UPDATE SET
               net_inr=excluded.net_inr, gross_inr=excluded.gross_inr, tax_inr=excluded.tax_inr,
               net_native=excluded.net_native, gross_native=excluded.gross_native, tax_native=excluded.tax_native
               WHERE status='PENDING'""",
            (x.symbol, str(x.ex_date), str(x.payment_date), x.net_inr, x.gross_inr, x.tax_inr,
             x.currency, x.net_native, x.gross_native, x.tax_native)
        )


def insert_snapshot(connection: sqlite3.Connection, day, portfolio: Portfolio, value: float) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",
        (str(day), value, portfolio.cash_inr, portfolio.realised_pnl_inr, portfolio.dividend_income_inr)
    )


def persist_result(result, connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO fills(order_id,date,symbol,side,quantity,price_native,fee_inr,reason) VALUES(NULL,?,?,?,?,?,?,?)",
        [(str(x.date), x.symbol, x.side, x.quantity, x.price_native, x.fee_inr, x.reason) for x in result.portfolio.fills]
    )
    connection.executemany(
        "INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?)",
        [(str(day), value, cash, realised, dividend) for day, value, cash, realised, dividend in result.snapshots]
    )
    connection.executemany(
        "INSERT INTO decisions(date,symbol,strategy,score,explanation) VALUES(?,?,?,?,?)",
        [("", x.symbol, x.strategy, x.score, x.explanation) for x in result.decisions]
    )
    connection.commit()
