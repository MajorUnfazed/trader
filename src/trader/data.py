from pathlib import Path
import pandas as pd

# `close` is the tradable, unadjusted close. `adj_close` is used only for
# return-based indicators and must be split/dividend adjusted by the vendor.
REQUIRED = {"date", "open", "high", "low", "close", "adj_close", "volume", "dividend"}

def read_price_csv(path: str | Path, symbol: str | None = None) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    missing = REQUIRED - set(df.columns)
    if missing: raise ValueError(f"{path}: missing columns {sorted(missing)}")
    df = df.sort_values("date").drop_duplicates("date").set_index("date")
    if df.index.has_duplicates or not df.index.is_monotonic_increasing: raise ValueError(f"{path}: dates must be unique and ordered")
    if df[list(REQUIRED - {"date"})].isna().any().any(): raise ValueError(f"{path}: missing market values")
    if (df[["open", "high", "low", "close", "adj_close"]] <= 0).any().any(): raise ValueError(f"{path}: prices must be positive")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any() or (df["low"] > df[["open", "close", "high"]].min(axis=1)).any(): raise ValueError(f"{path}: invalid OHLC range")
    if "split_factor" not in df: df["split_factor"] = 1.0
    if (df["split_factor"] <= 0).any(): raise ValueError(f"{path}: split_factor must be positive")
    # For a dividend row, `dividend_payment_date` is optional for backwards
    # compatibility. Production/backtest datasets should provide it.
    if "dividend_payment_date" in df: df["dividend_payment_date"] = pd.to_datetime(df["dividend_payment_date"], errors="coerce")
    if "tradable" in df:
        values = df["tradable"].astype(str).str.strip().str.lower()
        mapping = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
        parsed = values.map(mapping)
        if parsed.isna().any(): raise ValueError(f"{path}: tradable must contain true/false values")
        df["tradable"] = parsed.astype(bool)
    df["symbol"] = symbol or Path(path).stem
    return df

def load_directory(directory: str | Path) -> dict[str, pd.DataFrame]:
    paths = sorted(Path(directory).glob("*.csv"))
    if not paths: raise FileNotFoundError(f"No CSV files in {directory}")
    return {p.stem: read_price_csv(p) for p in paths}

def download_yfinance(symbols: list[str], start: str, end: str, output_dir: str | Path) -> None:
    """Research-only convenience adapter; caller is responsible for Yahoo licensing/quality."""
    try: import yfinance as yf
    except ImportError as e: raise RuntimeError("Install with: pip install -e '.[research]'") from e
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False, actions=True)
        if df.empty: raise ValueError(f"No data returned for {symbol}")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        splits = df["Stock Splits"] if "Stock Splits" in df else pd.Series(1.0, index=df.index)
        result = pd.DataFrame({"date": df.index, "open": df["Open"], "high": df["High"], "low": df["Low"], "close": df["Close"], "adj_close": df["Adj Close"], "volume": df["Volume"], "dividend": df.get("Dividends", 0.0), "split_factor": splits.replace(0, 1.0)})
        result.to_csv(out / f"{symbol}.csv", index=False)

def read_fx_csv(path: str | Path) -> pd.Series:
    """Read a USD/INR close-rate file with columns: date,usd_inr.

    Rates are forward-filled only across days where the FX market is closed;
    callers must reject dates before the first supplied rate.
    """
    df = pd.read_csv(path, parse_dates=["date"])
    if set(("date", "usd_inr")) - set(df.columns): raise ValueError(f"{path}: expected date,usd_inr")
    fx = df.sort_values("date").drop_duplicates("date").set_index("date")["usd_inr"].astype(float)
    if fx.isna().any() or (fx <= 0).any(): raise ValueError(f"{path}: USD/INR rates must be positive")
    return fx

def read_universe_membership_csv(path: str | Path) -> pd.DataFrame:
    """Read point-in-time universe intervals (inclusive start_date/end_date)."""
    df = pd.read_csv(path)
    required = {"symbol", "start_date", "end_date"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing membership columns {sorted(missing)}")
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    if df["symbol"].eq("").any() or df["start_date"].isna().any():
        raise ValueError(f"{path}: symbol and start_date are required")
    if (df["end_date"].notna() & (df["end_date"] < df["start_date"])).any():
        raise ValueError(f"{path}: end_date cannot precede start_date")
    for symbol, group in df.sort_values(["symbol", "start_date"]).groupby("symbol"):
        previous_end = None
        for row in group.itertuples():
            if previous_end is None and row.Index != group.index[0]:
                raise ValueError(f"{path}: overlapping membership intervals for {symbol}")
            if previous_end is not None and row.start_date <= previous_end:
                raise ValueError(f"{path}: overlapping membership intervals for {symbol}")
            previous_end = row.end_date
            if pd.isna(previous_end) and row.Index != group.index[-1]:
                raise ValueError(f"{path}: open-ended membership interval must be last for {symbol}")
    return df
