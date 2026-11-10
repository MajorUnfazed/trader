from pathlib import Path
import pandas as pd

# `close` is the tradable, unadjusted close. `adj_close` is vendor-adjusted.
REQUIRED = {"date", "open", "high", "low", "close", "adj_close", "volume", "dividend"}


def read_price_csv(path: str | Path, symbol: str | None = None) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    if df["date"].isna().any():
        raise ValueError(f"{path}: invalid market dates")
    if df["date"].duplicated().any():
        raise ValueError(f"{path}: duplicate market dates")
    df = df.sort_values("date").set_index("date")
    numeric_columns = ["open", "high", "low", "close", "adj_close", "volume", "dividend"]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if df[numeric_columns].isna().any().any():
        raise ValueError(f"{path}: missing or non-numeric market values")
    if (df[["open", "high", "low", "close", "adj_close"]] <= 0).any().any():
        raise ValueError(f"{path}: prices must be positive")
    if (df["volume"] < 0).any() or (df["dividend"] < 0).any():
        raise ValueError(f"{path}: volume and dividend values cannot be negative")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any() or (df["low"] > df[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{path}: invalid OHLC range")
    if "split_factor" not in df:
        df["split_factor"] = 1.0
    df["split_factor"] = pd.to_numeric(df["split_factor"], errors="coerce")
    if df["split_factor"].isna().any() or (df["split_factor"] <= 0).any():
        raise ValueError(f"{path}: split_factor must be positive")
    if "dividend_payment_date" in df:
        raw_payment_dates = df["dividend_payment_date"]
        parsed_payment_dates = pd.to_datetime(raw_payment_dates, errors="coerce")
        invalid_payment_dates = raw_payment_dates.notna() & raw_payment_dates.astype(str).str.strip().ne("") & parsed_payment_dates.isna()
        if invalid_payment_dates.any():
            raise ValueError(f"{path}: invalid dividend_payment_date values")
        df["dividend_payment_date"] = parsed_payment_dates
    if "delisting_price" in df:
        df["delisting_price"] = pd.to_numeric(df["delisting_price"], errors="coerce")
        invalid = df["delisting_price"].notna() & (df["delisting_price"] <= 0)
        if invalid.any():
            raise ValueError(f"{path}: delisting_price must be positive when supplied")
    if "tradable" in df:
        values = df["tradable"].astype(str).str.strip().str.lower()
        mapping = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
        parsed = values.map(mapping)
        if parsed.isna().any():
            raise ValueError(f"{path}: tradable must contain true/false values")
        df["tradable"] = parsed.astype(bool)
        if ((~df["tradable"]) & (df["volume"] != 0)).any():
            raise ValueError(f"{path}: non-tradable rows must have zero volume")
    df["symbol"] = symbol or Path(path).stem
    return df


def total_return_index(df: pd.DataFrame, base: float = 100.0) -> pd.Series:
    """Build a split- and cash-dividend-reinvested total-return index.

    Dividends are assumed to be per post-action share and reinvested at the
    ex-date close. This is a transparent proxy, not a substitute for a
    provider's verified total-return index or tax/withholding treatment.
    """
    if "close" not in df:
        raise ValueError("Total-return calculation requires close prices")
    close = pd.to_numeric(df["close"], errors="coerce")
    dividend = pd.to_numeric(df.get("dividend", 0.0), errors="coerce")
    if not isinstance(dividend, pd.Series):
        dividend = pd.Series(float(dividend), index=df.index)
    split = pd.to_numeric(df.get("split_factor", 1.0), errors="coerce")
    if not isinstance(split, pd.Series):
        split = pd.Series(float(split), index=df.index)
    if close.isna().any() or (close <= 0).any() or dividend.isna().any() or (dividend < 0).any() or split.isna().any() or (split <= 0).any():
        raise ValueError("Invalid close, dividend, or split data for total-return calculation")
    gross = ((close + dividend) * split) / close.shift(1)
    gross.iloc[0] = 1.0
    return base * gross.cumprod()


def load_directory(directory: str | Path) -> dict[str, pd.DataFrame]:
    paths = sorted(Path(directory).glob("*.csv"))
    if not paths:
        raise FileNotFoundError(f"No CSV files in {directory}")
    return {p.stem: read_price_csv(p) for p in paths}


def download_yfinance(symbols: list[str], start: str, end: str, output_dir: str | Path) -> None:
    """Research-only convenience adapter; caller is responsible for Yahoo licensing/quality."""
    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError("Install with: pip install -e '.[research]'") from e
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False, actions=True)
        if df.empty:
            raise ValueError(f"No data returned for {symbol}")
        df.index = pd.to_datetime(df.index).tz_localize(None)
        splits = df["Stock Splits"] if "Stock Splits" in df else pd.Series(1.0, index=df.index)
        result = pd.DataFrame({"date": df.index, "open": df["Open"], "high": df["High"], "low": df["Low"], "close": df["Close"], "adj_close": df["Adj Close"], "volume": df["Volume"], "dividend": df.get("Dividends", 0.0), "split_factor": splits.replace(0, 1.0)})
        result.to_csv(out / f"{symbol}.csv", index=False)


def read_fx_csv(path: str | Path) -> pd.Series:
    """Read a USD/INR close-rate file with columns: date,usd_inr."""
    df = pd.read_csv(path, parse_dates=["date"])
    if set(("date", "usd_inr")) - set(df.columns):
        raise ValueError(f"{path}: expected date,usd_inr")
    if df["date"].isna().any():
        raise ValueError(f"{path}: invalid FX dates")
    if df["date"].duplicated().any():
        raise ValueError(f"{path}: duplicate FX dates")
    fx = df.sort_values("date").set_index("date")["usd_inr"].astype(float)
    if fx.isna().any() or (fx <= 0).any():
        raise ValueError(f"{path}: USD/INR rates must be positive")
    return fx


def read_universe_membership_csv(path: str | Path) -> pd.DataFrame:
    """Read point-in-time universe intervals (inclusive start_date/end_date)."""
    df = pd.read_csv(path)
    required = {"symbol", "start_date", "end_date"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing membership columns {sorted(missing)}")
    if df["symbol"].isna().any() or df["start_date"].isna().any():
        raise ValueError(f"{path}: symbol and start_date are required")
    df["symbol"] = df["symbol"].astype(str).str.strip()
    raw_end = df["end_date"]
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(raw_end, errors="coerce")
    invalid_end = raw_end.notna() & raw_end.astype(str).str.strip().ne("") & df["end_date"].isna()
    if df["symbol"].eq("").any() or df["start_date"].isna().any() or invalid_end.any():
        raise ValueError(f"{path}: invalid symbol or membership dates")
    if (df["end_date"].notna() & (df["end_date"] < df["start_date"])).any():
        raise ValueError(f"{path}: end_date cannot precede start_date")
    for symbol, group in df.sort_values(["symbol", "start_date"]).groupby("symbol"):
        previous_end = None
        for index, row in enumerate(group.itertuples()):
            if index > 0 and (pd.isna(previous_end) or row.start_date <= previous_end):
                raise ValueError(f"{path}: overlapping membership intervals for {symbol}")
            previous_end = row.end_date
    return df
