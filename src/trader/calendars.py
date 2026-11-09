"""Exchange-session validation backed by pandas-market-calendars."""
from functools import lru_cache
import pandas as pd

DEFAULT_EXCHANGE = {"INR": "NSE", "USD": "NYSE"}

@lru_cache(maxsize=8)
def sessions(exchange: str, start, end) -> pd.DatetimeIndex:
    try:
        import pandas_market_calendars as mcal
    except ImportError as e:
        raise RuntimeError("Install project dependencies to use exchange calendars") from e
    calendar=mcal.get_calendar(exchange)
    schedule=calendar.schedule(start_date=pd.Timestamp(start).date(), end_date=pd.Timestamp(end).date())
    return pd.DatetimeIndex(schedule.index).tz_localize(None).normalize()

def valid_sessions(prices: dict[str,pd.DataFrame], metadata: pd.DataFrame, start, end) -> dict[str, pd.DatetimeIndex]:
    """Return valid sessions per instrument and reject vendor rows on holidays.

    `exchange` can be provided in fundamentals; otherwise a conservative
    currency mapping selects NSE for INR and NYSE for USD.
    """
    result={}
    for symbol,df in prices.items():
        row=metadata[metadata["symbol"] == symbol]
        currency=str(row.iloc[-1].get("currency","INR")) if not row.empty else "INR"
        exchange=str(row.iloc[-1].get("exchange",DEFAULT_EXCHANGE.get(currency,"NYSE"))) if not row.empty else DEFAULT_EXCHANGE.get(currency,"NYSE")
        expected=sessions(exchange,start,end)
        all_observed=pd.DatetimeIndex(df.index).normalize()
        observed=all_observed[(all_observed >= pd.Timestamp(start)) & (all_observed <= pd.Timestamp(end))]
        if len(observed) == 0:
            # The instrument may not yet be listed or may be outside the
            # requested window. It contributes no sessions to this run.
            result[symbol] = pd.DatetimeIndex([])
            continue
        invalid=observed.difference(expected)
        if len(invalid): raise ValueError(f"{symbol}: data contains non-session dates for {exchange}: {invalid[:3].tolist()}")
        active=expected[(expected >= observed.min()) & (expected <= observed.max())]
        missing=active.difference(observed)
        if len(missing): raise ValueError(f"{symbol}: missing quotes on {exchange} sessions: {missing[:3].tolist()}")
        result[symbol]=expected
    return result
