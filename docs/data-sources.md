# Data-source decision record

This project is paper-only. A data adapter is **not** a licence to download,
retain, display, or redistribute market data. Record the order form, plan,
exchange entitlements, retrieval time, API response/version, and any manual
corrections beside every dataset. The importer writes a `*.csv.metadata.json`
sidecar for this purpose; it does not validate a vendor's licence.

## Recommendation for a small experiment

Use exchange/regulator files and manual CSV import as the default, not an
unattended scraper. Start with a small, disclosed current universe and label
results *coverage-limited*. For ongoing paper marks, use a paid EOD vendor only
after confirming its exchange terms. Do not advertise, share, or publish the
resulting files without the provider's written redistribution permission.

| Need | India (NSE/BSE) | US (NYSE/Nasdaq) | Practical choice and limitation |
| --- | --- | --- | --- |
| Official EOD prices / actions | NSE Data & Analytics paid EOD/historical and corporate data; BSE Information Products EOD, historical and corporate data | Exchange feeds through an entitled vendor | Best provenance, but quotes/historical files are licensed and price/terms change. Public bhav copies are useful checks, not a guaranteed survivor-bias-free research universe. |
| Low-cost EOD prototype | Imported exchange/vendor CSV | Tiingo EOD | Tiingo supplies raw and adjusted OHLCV plus dividend and split factors. Its free/individual plans are internal-use only; it is not a substitute for CRSP's inactive security history. |
| One API across countries | Verify EODHD's `NSE`/`BSE` exchange symbols and plan before purchase | EODHD or Tiingo | EODHD exposes EOD, dividends, splits and fundamentals, but plan entitlement, history depth, point-in-time fields, corrections, and redistribution must be confirmed in writing. |
| Delisted securities + historical constituents | Licensed exchange/reference data; retain constituent circulars and settlement notices | CRSP US Stock & Indexes, usually via an academic/institutional subscription | CRSP is the practical research-grade US option: active/inactive issues, permanent identifiers, corporate actions and delisting returns. There is no cheap API that makes this complete for both India and US. |
| Point-in-time fundamentals | Exchange announcements / annual reports; licensed corporate-data feed for scale | SEC EDGAR submissions + XBRL accepted/filing timestamps; vendor/Compustat for standardized history | SEC is free and has no API key, but issuer tags can be non-comparable and the company-facts endpoint is not a complete point-in-time restatement database. Preserve accession/accepted time and original facts. |
| Total-return benchmarks | NSE Indices / BSE Indices licensed total-return series | S&P DJI / CRSP or other licensed total-return index series | Do not call a price index a total-return index. The code's dividend-reinvestment calculation is only an auditable proxy. |
| USD/INR | RBI archive before 2018; FBIL reference rates thereafter | Same | Reference rates are daily benchmarks, not tradable broker FX execution rates; align dates explicitly. |

### Provider facts to validate before subscribing

1. **Adjusted OHLCV:** Ask whether `adj_close` includes cash dividends and
   splits, whether it is back-adjusted after corrections, and whether raw OHLC
   remains available. Tiingo documents both raw and adjusted prices and labels
   dividends as ex-date events; this project preserves both rather than trying
   to reverse-engineer an adjusted series.
2. **Corporate actions:** Obtain ex-date, amount/ratio, currency, record date,
   payment date, announcement/publication time, and source identifier. A
   dividend amount alone is insufficient for a cash timeline. EODHD documents
   payment dates in its corporate-actions API, but that remains provider data
   subject to plan entitlement.
3. **Survivorship:** Require an inactive/delisted-security flag, stable security
   identifier, historical symbol/name mappings, historical membership intervals,
   and a post-delisting cash/return field. For US research CRSP documents an
   explicit delisting return/payment date; a last observed quote is not a
   delisting return. Indian delisting settlement values should be reconciled to
   exchange/company notices.
4. **Fundamentals:** Require period end *and* filing/acceptance/publication
   timestamp. `available_at` is the only timestamp this project uses for
   decision-time selection; fiscal period end alone is rejected as a claim of
   availability.
5. **Limits and cost:** Capture the plan name, request/day limits, exchange
   add-ons, user/display/redistribution category, and renewal date in the
   sidecar `license_note`. Tiingo lists plan-specific request limits and
   internal-use licences; EODHD counts API calls by endpoint. Neither public
   web page is a permanent price quote. NSE/BSE tariffs and terms can change.

## Implemented workflow

The default `csv` source is intentional. Import a saved vendor or exchange
export and identify it:

```powershell
python -m trader.cli import-prices --input vendor-export.csv --output data/raw/RELIANCE.csv `
  --provider nse-data-analytics --symbol RELIANCE --source-url "order/SFTP export reference" `
  --license-note "Internal research only; agreement ref and retrieval log retained separately"
```

The importer canonicalises common column names, rejects duplicate dates and
invalid prices, retains raw `close` and provider `adj_close`, treats a zero
split value as “no event”, and writes provenance beside the normalized file.
It never fabricates payment dates, adjustment conventions, delisting values,
or missing prices.

Remote adapters are intentionally narrow and opt-in:

```powershell
$env:TRADER_TIINGO_API_KEY = "your-key"
python -m trader.cli fetch-prices --provider tiingo --symbol AAPL --start 2020-01-01 --end 2024-12-31 --output data/raw/AAPL.csv
```

`tiingo` (US EOD) and `eodhd` (global EOD) are implemented only after the user
sets the corresponding `TRADER_<PROVIDER>_API_KEY`. No credentials are bundled,
and neither adapter accesses corporate-action or fundamentals endpoints beyond
the user's purchased entitlement. Continue to import licensed delisting,
constituent, benchmark total-return, FX, and point-in-time fundamental files.

## Primary documentation consulted (accessed 2026-11-11)

- [NSE paid EOD/historical data](https://betanseapi.nseindia.com/static/market-data/eod-historical-data-subscription)
- [NSE Data & Analytics products](https://www.nseindia.com/static/nse-data-and-analytics)
- [BSE information-products tariff](https://www.bseindia.com/downloads1/Information_Products_Pricing_Sheet.pdf)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [SEC EDGAR access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [CRSP US Stock & Indexes coverage](https://www.crsp.org/research/crsp-us-stock-databases-2025-layout/)
- [CRSP delisting-return definitions](https://www.crsp.org/crsp_pdf/crsp-us-stock-indexes-databases-data-descriptions-guide-crspaccess/)
- [Tiingo EOD fields and adjustment method](https://www.tiingo.com/documentation/end-of-day)
- [Tiingo plan limits/licensing summary](https://www.tiingo.com/products/iex-api)
- [EODHD corporate-actions API](https://eodhd.com/financial-apis/api-splits-dividends)
- [EODHD bulk/API entitlement details](https://eodhd.com/financial-apis/bulk-api-eod-splits-dividends)
- [RBI reference-rate archive](https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx) and [RBI notice of FBIL takeover](https://rbi.org.in/scripts/FS_RTI_Diclosure_Logs.aspx?fn=6)
