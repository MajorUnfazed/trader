import json

import pandas as pd
import pytest

from trader.data import read_fundamentals_csv, read_price_csv
from trader.providers import Provenance, import_price_csv, normalize_corporate_actions, normalize_prices, provider_from_config


def test_vendor_price_aliases_normalize_without_losing_actions():
    source = pd.DataFrame({
        "Date": ["2024-01-03", "2024-01-02"], "Open": [11, 10], "High": [12, 11],
        "Low": [10, 9], "Close": [11, 10], "Adj Close": [10.8, 9.8], "Volume": [20, 10],
        "Dividends": [0.2, 0], "Stock Splits": [0, 1],
    })
    result = normalize_prices(source, symbol="ABC")
    assert list(result["date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert list(result["split_factor"]) == [1, 1]
    assert result.loc[result.index[-1], "dividend"] == 0.2


def test_import_writes_provenance_and_reader_loads_it(tmp_path):
    source = tmp_path / "source.csv"
    pd.DataFrame({"date": ["2024-01-02"], "open": [10], "high": [11], "low": [9],
                  "close": [10], "adj_close": [10], "volume": [100], "dividend": [0]}).to_csv(source, index=False)
    destination = tmp_path / "ABC.csv"
    import_price_csv(source, destination, Provenance.now("test-provider", license_note="internal use"), symbol="ABC")
    metadata = json.loads((tmp_path / "ABC.csv.metadata.json").read_text())
    assert metadata["provider"] == "test-provider" and metadata["raw_sha256"]
    assert read_price_csv(destination).attrs["provenance"]["normalized_sha256"] == metadata["normalized_sha256"]


def test_actions_require_explicit_event_dates_and_types():
    events = normalize_corporate_actions(pd.DataFrame({"event_type": ["dividend"], "ex_date": ["2024-01-02"], "amount": [1]}))
    assert events.iloc[0]["event_type"] == "dividend"
    with pytest.raises(ValueError, match="event_type"):
        normalize_corporate_actions(pd.DataFrame({"event_type": ["merger"], "ex_date": ["2024-01-02"]}))


def test_fundamentals_use_timestamp_and_reject_invalid_timestamp(tmp_path):
    good = tmp_path / "fundamentals.csv"
    pd.DataFrame({"symbol": ["ABC"], "as_of_date": ["2023-12-31"], "published_at": ["2024-02-01T12:00:00Z"]}).to_csv(good, index=False)
    assert "available_at" in read_fundamentals_csv(good)
    bad = tmp_path / "bad.csv"
    pd.DataFrame({"symbol": ["ABC"], "as_of_date": ["2023-12-31"], "available_at": ["nope"]}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="available_at"):
        read_fundamentals_csv(bad)


def test_remote_provider_refuses_missing_credential(monkeypatch):
    monkeypatch.delenv("TRADER_TIINGO_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="requires an API key"):
        provider_from_config("tiingo").prices("AAPL", "2024-01-01", "2024-01-02")
