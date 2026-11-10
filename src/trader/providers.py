"""Explicit market-data provider contracts and file-normalisation utilities.

Adapters do not run automatically: callers must provide their own API key and
must be entitled to download, store, and use the requested data.  This module
is deliberately for research/paper trading; it is not a market-data redistributor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd


CANONICAL_PRICE_COLUMNS = ("date", "open", "high", "low", "close", "adj_close", "volume", "dividend")
_ALIASES = {
    "date": ("date", "datetime", "timestamp"),
    "open": ("open",), "high": ("high",), "low": ("low",), "close": ("close",),
    "adj_close": ("adj_close", "adjusted_close", "adjclose", "adj. close"),
    "volume": ("volume", "vol"),
    "dividend": ("dividend", "dividends", "div_cash", "divcash"),
    "split_factor": ("split_factor", "splitfactor", "stock splits", "split"),
    "dividend_payment_date": ("dividend_payment_date", "payment_date", "paymentdate"),
}


@dataclass(frozen=True)
class Provenance:
    """A sidecar record required for a reproducible local dataset."""
    provider: str
    retrieved_at: str
    source_url: str | None = None
    license_note: str | None = None
    dataset_version: str | None = None
    adjustment_basis: str = "provider_supplied"
    raw_sha256: str | None = None
    notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(cls, provider: str, **kwargs: Any) -> "Provenance":
        return cls(provider=provider, retrieved_at=datetime.now(timezone.utc).isoformat(), **kwargs)


class MarketDataProvider(Protocol):
    name: str
    def prices(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...
    def corporate_actions(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...


def _normalise_name(name: str) -> str:
    return " ".join(str(name).strip().lower().replace("_", " ").split())


def normalize_prices(frame: pd.DataFrame, *, symbol: str | None = None) -> pd.DataFrame:
    """Map common vendor CSV/JSON headings to the project's canonical schema."""
    rename: dict[str, str] = {}
    available = {_normalise_name(c): c for c in frame.columns}
    for target, aliases in _ALIASES.items():
        found = next((available.get(_normalise_name(alias)) for alias in aliases if _normalise_name(alias) in available), None)
        if found:
            rename[found] = target
    result = frame.rename(columns=rename).copy()
    missing = set(CANONICAL_PRICE_COLUMNS) - set(result.columns)
    # A raw, unadjusted-only feed remains usable for execution but must not be
    # silently claimed to be total-return adjusted.
    if missing == {"adj_close"} and "close" in result:
        result["adj_close"] = result["close"]
        result.attrs["adjustment_warning"] = "No adjusted close supplied; copied raw close. Do not use for total-return research."
        missing = set()
    if missing:
        raise ValueError(f"price input is missing canonical columns {sorted(missing)}")
    result["date"] = pd.to_datetime(result["date"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    if result["date"].isna().any() or result["date"].duplicated().any():
        raise ValueError("price input has invalid or duplicate dates")
    for column in ("open", "high", "low", "close", "adj_close", "volume", "dividend"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if result[list(CANONICAL_PRICE_COLUMNS)[1:]].isna().any().any():
        raise ValueError("price input contains non-numeric canonical values")
    if "split_factor" not in result:
        result["split_factor"] = 1.0
    # Some exports encode no split as zero; zero is not a valid multiplicative factor.
    result["split_factor"] = pd.to_numeric(result["split_factor"], errors="coerce").replace(0, 1.0)
    if result["split_factor"].isna().any() or (result["split_factor"] <= 0).any():
        raise ValueError("split_factor must be positive")
    if symbol:
        result["symbol"] = symbol
    ordered = [c for c in (*CANONICAL_PRICE_COLUMNS, "split_factor") if c in result]
    return result[ordered + [c for c in result.columns if c not in ordered]].sort_values("date")


def normalize_corporate_actions(frame: pd.DataFrame, *, symbol: str | None = None) -> pd.DataFrame:
    """Validate explicit corporate-action events without inferring missing dates."""
    result = frame.rename(columns={c: _normalise_name(c).replace(" ", "_") for c in frame.columns}).copy()
    required = {"event_type", "ex_date"}
    if missing := required - set(result.columns):
        raise ValueError(f"corporate-action input missing {sorted(missing)}")
    result["event_type"] = result["event_type"].astype(str).str.lower().str.strip()
    if not result["event_type"].isin({"dividend", "split", "delisting"}).all():
        raise ValueError("event_type must be dividend, split, or delisting")
    for date_column in ("ex_date", "payment_date", "announcement_date"):
        if date_column in result:
            result[date_column] = pd.to_datetime(result[date_column], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    if result["ex_date"].isna().any():
        raise ValueError("corporate actions have invalid ex_date")
    if symbol:
        result["symbol"] = symbol
    return result.sort_values(["ex_date", "event_type"]).reset_index(drop=True)


def normalize_fundamentals(frame: pd.DataFrame) -> pd.DataFrame:
    """Require a decision-time availability timestamp when supplied by a source."""
    result = frame.copy()
    aliases = {"published_at": "available_at", "filing_date": "available_at", "accepted_at": "available_at"}
    for old, new in aliases.items():
        if old in result and new not in result:
            result = result.rename(columns={old: new})
    if {"symbol", "as_of_date"} - set(result.columns):
        raise ValueError("fundamentals require symbol and as_of_date")
    result["as_of_date"] = pd.to_datetime(result["as_of_date"], errors="coerce").dt.normalize()
    if result["as_of_date"].isna().any():
        raise ValueError("fundamentals have invalid as_of_date")
    if "available_at" in result:
        result["available_at"] = pd.to_datetime(result["available_at"], errors="coerce", utc=True)
        if result["available_at"].isna().any():
            raise ValueError("fundamentals have invalid available_at")
    return result


def write_dataset(path: str | Path, frame: pd.DataFrame, provenance: Provenance) -> Path:
    """Write canonical CSV plus an adjacent immutable provenance record."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
    payload = asdict(provenance)
    payload["normalized_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".metadata.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return destination


def import_price_csv(input_path: str | Path, output_path: str | Path, provenance: Provenance, *, symbol: str | None = None) -> Path:
    raw = Path(input_path).read_bytes()
    normal = normalize_prices(pd.read_csv(input_path), symbol=symbol)
    provenance = Provenance(**{**asdict(provenance), "raw_sha256": hashlib.sha256(raw).hexdigest()})
    return write_dataset(output_path, normal, provenance)


class _JsonHTTPProvider:
    name = "base"
    def __init__(self, api_key: str | None = None, *, timeout_seconds: int = 30):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
    def _key(self) -> str:
        if not self.api_key:
            raise RuntimeError(f"{self.name} requires an API key. Set it in config or the documented environment variable; no key is bundled.")
        return self.api_key
    def _get_json(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": "longterm-trader/0.1 research-only"})
        with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: URL is provider-controlled
            return json.loads(response.read().decode("utf-8"))


class TiingoProvider(_JsonHTTPProvider):
    """US EOD adapter. Tiingo access and use are subject to its plan/license."""
    name = "tiingo"
    def prices(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        query = urlencode({"startDate": start, "endDate": end, "token": self._key()})
        rows = self._get_json(f"https://api.tiingo.com/tiingo/daily/{symbol}/prices?{query}")
        return normalize_prices(pd.DataFrame(rows), symbol=symbol)
    def corporate_actions(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        raise NotImplementedError("Use the provider's entitled corporate-action endpoint and import its export; API availability varies by plan.")


class EODHDProvider(_JsonHTTPProvider):
    """Global EOD adapter; coverage and redistribution are plan-dependent."""
    name = "eodhd"
    def prices(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        query = urlencode({"from": start, "to": end, "fmt": "json", "api_token": self._key()})
        rows = self._get_json(f"https://eodhd.com/api/eod/{symbol}?{query}")
        return normalize_prices(pd.DataFrame(rows), symbol=symbol)
    def corporate_actions(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        query = urlencode({"from": start, "to": end, "fmt": "json", "api_token": self._key()})
        rows = self._get_json(f"https://eodhd.com/api/div/{symbol}?{query}")
        events = pd.DataFrame(rows).rename(columns={"date": "ex_date", "paymentDate": "payment_date", "value": "amount"})
        events["event_type"] = "dividend"
        return normalize_corporate_actions(events, symbol=symbol)


def provider_from_config(name: str, api_key: str | None = None) -> MarketDataProvider:
    """Construct an opt-in remote adapter, sourcing keys only from user configuration."""
    key = api_key or os.getenv(f"TRADER_{name.upper()}_API_KEY")
    providers = {"tiingo": TiingoProvider, "eodhd": EODHDProvider}
    try:
        return providers[name.lower()](key)
    except KeyError as exc:
        raise ValueError(f"Unsupported provider {name!r}; supported remote adapters: {sorted(providers)}") from exc


def fetch_prices_with_fallback(
    providers: list[str], symbol: str | dict[str, str], start: str, end: str,
) -> tuple[pd.DataFrame, str]:
    """Fetch from the first provider, advancing only after an HTTP 429.

    A malformed symbol, missing entitlement, or bad credential must not silently
    move to a different source: those conditions need a user correction.  The
    caller is responsible for using the provider-specific symbol convention
    (for example, EODHD commonly uses ``AAPL.US`` rather than ``AAPL``).
    """
    if not providers:
        raise ValueError("At least one provider is required")
    for index, name in enumerate(providers):
        try:
            provider_symbol = symbol.get(name, symbol.get("default", "")) if isinstance(symbol, dict) else symbol
            if not provider_symbol:
                raise ValueError(f"No symbol supplied for provider {name!r}")
            return provider_from_config(name).prices(provider_symbol, start, end), name
        except HTTPError as exc:
            if exc.code != 429 or index == len(providers) - 1:
                raise
    raise RuntimeError("No provider was attempted")  # defensive: loop always returns or raises
