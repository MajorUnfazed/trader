from dataclasses import dataclass, field
from datetime import date
import math

@dataclass
class Position:
    symbol: str; quantity: int; avg_cost_native: float; avg_cost_inr: float; currency: str; sector: str = "Unknown"

@dataclass
class Fill:
    symbol: str; side: str; quantity: int; price_native: float; fee_inr: float; date: date; reason: str

@dataclass
class DividendReceivable:
    symbol: str; ex_date: date; payment_date: date; net_inr: float; gross_inr: float; tax_inr: float

@dataclass
class Portfolio:
    cash_inr: float
    positions: dict[str, Position] = field(default_factory=dict)
    realised_pnl_inr: float = 0.0
    dividend_income_inr: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    last_prices: dict[str, float] = field(default_factory=dict)
    dividend_receivables: list[DividendReceivable] = field(default_factory=list)

    def fx(self, currency: str, usd_inr: float) -> float: return usd_inr if currency == "USD" else 1.0
    def mark(self, quotes: dict[str, float]) -> None:
        self.last_prices.update(quotes)
    def value(self, quotes: dict[str, float], currencies: dict[str,str], usd_inr: float) -> float:
        self.mark(quotes)
        return self.cash_inr + sum(p.quantity * self.last_prices.get(s, p.avg_cost_native) * self.fx(currencies.get(s,p.currency),usd_inr) for s,p in self.positions.items())
    def buy(self, symbol, price, currency, sector, budget_inr, cfg, when, reason, usd_inr=None) -> Fill | None:
        fx=self.fx(currency,usd_inr or cfg.usd_inr); slip=1+cfg.slippage_bps/10000; px=price*slip
        cost_rate=(cfg.us_cost_bps if currency=="USD" else cfg.india_cost_bps)/10000
        qty=math.floor(budget_inr/(px*fx*(1+cost_rate)))
        if qty < 1: return None
        gross=qty*px*fx; fee=gross*cost_rate
        if gross+fee > self.cash_inr + 1e-8: return None
        old=self.positions.get(symbol)
        if old:
            old.avg_cost_native=(old.avg_cost_native*old.quantity+px*qty)/(old.quantity+qty)
            old.avg_cost_inr=(old.avg_cost_inr*old.quantity+gross+fee)/(old.quantity+qty); old.quantity+=qty
        else: self.positions[symbol]=Position(symbol,qty,px,(gross+fee)/qty,currency,sector)
        self.cash_inr-=gross+fee; fill=Fill(symbol,"BUY",qty,px,fee,when,reason); self.fills.append(fill); return fill
    def sell(self, symbol, price, cfg, when, reason, quantity=None, usd_inr=None) -> Fill | None:
        p=self.positions.get(symbol)
        if not p: return None
        qty=min(quantity or p.quantity,p.quantity); fx=self.fx(p.currency,usd_inr or cfg.usd_inr); px=price*(1-cfg.slippage_bps/10000)
        rate=(cfg.us_cost_bps if p.currency=="USD" else cfg.india_cost_bps)/10000; gross=qty*px*fx; fee=gross*rate
        gain=gross-(p.avg_cost_inr*qty); tax=max(0,gain)*cfg.india_capital_gains_tax_rate
        self.cash_inr+=gross-fee-tax; self.realised_pnl_inr+=gain-fee-tax; p.quantity-=qty
        if not p.quantity: del self.positions[symbol]
        fill=Fill(symbol,"SELL",qty,px,fee+tax,when,reason); self.fills.append(fill); return fill
    def declare_dividend(self, symbol, per_share_native, ex_date, payment_date, cfg, usd_inr=None) -> DividendReceivable | None:
        """Create a receivable for ex-date holders; cash is credited on payment date."""
        p=self.positions.get(symbol)
        if not p: return None
        gross=p.quantity*per_share_native*self.fx(p.currency,usd_inr or cfg.usd_inr); tax=gross*cfg.us_withholding_tax_rate if p.currency=="USD" else 0
        receivable=DividendReceivable(symbol,ex_date,payment_date,gross-tax,gross,tax); self.dividend_receivables.append(receivable); return receivable
    def pay_dividends(self, when: date) -> list[DividendReceivable]:
        due=[x for x in self.dividend_receivables if x.payment_date <= when]
        self.dividend_receivables=[x for x in self.dividend_receivables if x.payment_date > when]
        for x in due: self.cash_inr+=x.net_inr; self.dividend_income_inr+=x.net_inr
        return due
    def apply_split(self, symbol: str, factor: float) -> None:
        """Apply a vendor-supplied new-shares-per-old-share ratio before trading."""
        p=self.positions.get(symbol)
        if p and factor != 1:
            new_quantity = p.quantity * factor
            if not float(new_quantity).is_integer(): raise ValueError(f"Non-integral split quantity for {symbol}")
            p.quantity = int(new_quantity); p.avg_cost_native /= factor
            if symbol in self.last_prices: self.last_prices[symbol] /= factor
