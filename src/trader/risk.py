"""Deterministic portfolio drift controls for paper/backtest execution."""
import math

def drift_targets(portfolio, quotes, currencies, usd_inr, risk) -> dict[str, tuple[float, str]]:
    """Return reduced target weights for names breaching position/sector caps.

    Targets are reductions only: the engine never adds risk solely because of
    drift. The rebalance threshold prevents micro-trading around a boundary.
    """
    total=portfolio.value(quotes,currencies,usd_inr)
    if total <= 0: return {}
    values={s:p.quantity*portfolio.last_prices.get(s,p.avg_cost_native)*portfolio.fx(currencies.get(s,p.currency),usd_inr) for s,p in portfolio.positions.items()}
    result={}
    for symbol,value in values.items():
        weight=value/total
        if weight-risk.max_position_weight >= risk.rebalance_threshold:
            result[symbol]=(risk.max_position_weight,"Position weight cap breached")
    sectors={}
    for symbol,value in values.items(): sectors.setdefault(portfolio.positions[symbol].sector,[]).append((symbol,value))
    for holdings in sectors.values():
        excess=sum(x[1] for x in holdings)/total-risk.max_sector_weight
        for symbol,value in sorted(holdings,key=lambda x:x[1],reverse=True):
            if excess <= risk.rebalance_threshold: break
            current=value/total; reduction=min(current,excess); target=max(0,current-reduction)
            prior=result.get(symbol,(current,""))[0]
            result[symbol]=(min(prior,target),"Sector weight cap breached")
            excess-=reduction
    return result

def quantity_to_target(position, price, currency_fx, total_value, target_weight) -> int:
    desired=math.floor(total_value*target_weight/(price*currency_fx))
    return max(0,position.quantity-desired)
