"""Pre-flight dataset coverage and provenance checks."""
from __future__ import annotations

import pandas as pd


def audit_data(
    prices: dict[str, pd.DataFrame],
    fundamentals: pd.DataFrame,
    universe_membership: pd.DataFrame | None = None,
    usd_inr: pd.Series | None = None,
) -> dict:
    if not prices:
        raise ValueError("No price histories supplied")
    if not {"symbol", "as_of_date"}.issubset(fundamentals.columns):
        raise ValueError("Fundamentals require symbol and as_of_date columns")
    price_symbols = set(prices)
    fundamental_symbols = set(fundamentals["symbol"].dropna().astype(str))
    missing_fundamentals = sorted(price_symbols - fundamental_symbols)
    currencies = {}
    if "currency" in fundamentals.columns:
        latest = fundamentals.sort_values("as_of_date").groupby("symbol").tail(1)
        currencies = dict(zip(latest["symbol"].astype(str), latest["currency"].astype(str).str.upper()))
    usd_symbols = sorted(symbol for symbol in price_symbols if currencies.get(symbol) == "USD")
    warnings = []
    if universe_membership is None:
        warnings.append("No historical universe membership supplied; survivorship bias may remain.")
    if "available_at" not in fundamentals.columns:
        warnings.append("Fundamentals have no available_at publication timestamps; as_of_date is being used as the availability proxy.")
    if missing_fundamentals:
        warnings.append("Some price symbols have no fundamentals and cannot generate fundamental strategy signals.")
    if usd_symbols and usd_inr is None:
        warnings.append("USD instruments have no historical USD/INR file; INR backtests will use the configured static FX rate.")
    if not any("delisting_price" in frame.columns and frame["delisting_price"].notna().any() for frame in prices.values()):
        warnings.append("No explicit delisting settlement prices were found; ensure historical delisted holdings are represented and settled from a verified source.")

    coverage = {}
    for symbol, frame in sorted(prices.items()):
        coverage[symbol] = {
            "rows": int(len(frame)),
            "start": str(frame.index.min().date()),
            "end": str(frame.index.max().date()),
            "halted_rows": int((~frame["tradable"].astype(bool)).sum()) if "tradable" in frame else 0,
            "dividend_events": int((pd.to_numeric(frame["dividend"], errors="coerce") > 0).sum()) if "dividend" in frame else 0,
            "split_events": int((pd.to_numeric(frame["split_factor"], errors="coerce") != 1).sum()) if "split_factor" in frame else 0,
            "delisting_settlements": int(frame["delisting_price"].notna().sum()) if "delisting_price" in frame else 0,
            "currency": currencies.get(symbol, "unknown"),
        }
    fx_range = None
    if usd_inr is not None and len(usd_inr):
        fx_range = {"start": str(usd_inr.index.min().date()), "end": str(usd_inr.index.max().date()), "observations": int(len(usd_inr))}
    return {
        "status": "WARN" if warnings else "OK",
        "symbols": len(prices),
        "fundamental_symbols": len(fundamental_symbols),
        "fundamental_snapshots": int(len(fundamentals)),
        "membership_supplied": universe_membership is not None,
        "fx_coverage": fx_range,
        "coverage": coverage,
        "warnings": warnings,
        "note": "This is a structural pre-flight audit, not proof that vendor data is accurate, licensed, point-in-time, or complete.",
    }
