from dataclasses import dataclass
import pandas as pd

@dataclass(frozen=True)
class Signal:
    symbol: str; strategy: str; score: float; explanation: str

def fundamentals_as_of(fundamentals: pd.DataFrame, decision_date) -> pd.DataFrame:
    """One latest publicly available fundamental snapshot per symbol."""
    required={"symbol","as_of_date"}
    if missing:=required-set(fundamentals.columns): raise ValueError(f"Missing fundamentals columns: {sorted(missing)}")
    f=fundamentals.copy(); f["as_of_date"]=pd.to_datetime(f["as_of_date"], errors="coerce")
    if f["as_of_date"].isna().any() or f["symbol"].isna().any():
        raise ValueError("Fundamentals require valid symbol and as_of_date values")
    if f.duplicated(["symbol", "as_of_date"]).any():
        raise ValueError("Duplicate point-in-time fundamentals snapshots for a symbol/date")
    f=f[f["as_of_date"] <= pd.Timestamp(decision_date)].sort_values(["symbol","as_of_date"])
    return f.groupby("symbol",as_index=True).tail(1).set_index("symbol")

def _num(row, col, default=0.0): return float(row.get(col, default) if pd.notna(row.get(col, default)) else default)

def dividend_quality(fundamentals: pd.DataFrame) -> list[Signal]:
    out=[]
    for symbol, r in fundamentals.iterrows():
        y,p,d,fcf = _num(r,"dividend_yield"),_num(r,"payout_ratio",1),_num(r,"debt_to_equity",99),bool(r.get("fcf_positive",False))
        if .015 <= y <= .08 and p <= .75 and d <= 1.5 and fcf:
            score = min(100, 35*y*1000 + 25*(1-p) + 20*(1/(1+d)))
            out.append(Signal(symbol,"dividend_quality",score,f"yield {y:.1%}; payout {p:.1%}; positive FCF; D/E {d:.2f}"))
    return out

def dividend_growth(fundamentals: pd.DataFrame) -> list[Signal]:
    out=[]
    for symbol,r in fundamentals.iterrows():
        g,y,p = _num(r,"dividend_growth_5y"),_num(r,"dividend_yield"),_num(r,"payout_ratio",1)
        if g >= .05 and y >= .01 and p <= .75:
            out.append(Signal(symbol,"dividend_growth",min(100,g*500+y*1000),f"5Y dividend growth {g:.1%}; yield {y:.1%}; payout {p:.1%}"))
    return out

def long_term_growth(prices: dict[str,pd.DataFrame], as_of: pd.Timestamp) -> list[Signal]:
    out=[]
    for symbol, df in prices.items():
        history = df.loc[:as_of]
        if "tradable" in history:
            history = history[history["tradable"].astype(bool)]
        h=history["adj_close"].dropna()
        if len(h) < 252: continue
        last,ma50,ma200=h.iloc[-1],h.iloc[-50:].mean(),h.iloc[-200:].mean()
        mom=h.iloc[-1]/h.iloc[-126]-1
        peak=h.cummax(); dd=(h/peak-1).min()
        if last > ma50 > ma200 and mom > 0 and dd > -.40:
            out.append(Signal(symbol,"long_term_growth",min(100,50+mom*100-dd*50),f"price above 50/200d MA; 6M momentum {mom:.1%}; trailing max drawdown {dd:.1%}"))
    return out

def target_weights(signals: list[Signal], allocations: dict, max_position_weight: float) -> dict[str, tuple[float, str]]:
    """Allocate each strategy sleeve independently; overlap is capped per name."""
    targets: dict[str, tuple[float, str]] = {}
    for strategy, sleeve in allocations.items():
        if strategy == "cash_reserve" or sleeve <= 0: continue
        ranked=sorted((x for x in signals if x.strategy == strategy), key=lambda x:x.score, reverse=True)
        slots=max(1, round(sleeve / max_position_weight))
        for sig in ranked[:slots]:
            previous, explanation=targets.get(sig.symbol,(0.0,""))
            targets[sig.symbol]=(min(max_position_weight,previous+sleeve/slots), "; ".join(x for x in (explanation,sig.explanation) if x))
    return targets

def universe_as_of(membership: pd.DataFrame, decision_date) -> set[str]:
    """Return constituents whose inclusive membership interval contains a date."""
    day = pd.Timestamp(decision_date)
    active = membership[
        (membership["start_date"] <= day)
        & (membership["end_date"].isna() | (membership["end_date"] >= day))
    ]
    return set(active["symbol"].astype(str))
