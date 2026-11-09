from dataclasses import dataclass, field
from pathlib import Path
import yaml

@dataclass
class RiskConfig:
    max_position_weight: float = .10
    max_sector_weight: float = .30
    min_cash_weight: float = .20
    rebalance_threshold: float = .03

@dataclass
class ExecutionConfig:
    india_cost_bps: float = 15
    us_cost_bps: float = 10
    slippage_bps: float = 10
    india_capital_gains_tax_rate: float = .10
    us_withholding_tax_rate: float = .25
    usd_inr: float = 83.0

@dataclass
class Config:
    base_currency: str = "INR"
    initial_capital_inr: float = 100_000
    allocations: dict = field(default_factory=lambda: {"dividend_quality": .30, "dividend_growth": .20, "long_term_growth": .30, "cash_reserve": .20})
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    def __post_init__(self):
        total=sum(float(v) for v in self.allocations.values())
        if abs(total-1.0) > 1e-9: raise ValueError(f"allocations must sum to 1.0, got {total}")
        if self.allocations.get("cash_reserve",0) < self.risk.min_cash_weight: raise ValueError("cash_reserve must be at least min_cash_weight")

def load_config(path: str = "config.yml") -> Config:
    p = Path(path)
    if not p.exists(): return Config()
    raw = yaml.safe_load(p.read_text()) or {}
    return Config(**{**raw, "risk": RiskConfig(**raw.get("risk", {})), "execution": ExecutionConfig(**raw.get("execution", {}))})
