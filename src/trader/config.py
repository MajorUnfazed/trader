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
    max_volume_participation: float = 0.01
    india_capital_gains_tax_rate: float = .10
    us_withholding_tax_rate: float = .25
    usd_inr: float = 83.0
    use_volatility_slippage: bool = False

@dataclass
class TaxConfig:
    india_stcg_rate: float = 0.20
    india_ltcg_rate: float = 0.125
    india_stcg_days: int = 365
    us_stcg_days: int = 730
    us_stcg_rate: float = 0.20
    us_ltcg_rate: float = 0.125
    enable_tax_lot_fifo: bool = False

@dataclass
class DataSourceConfig:
    """Provider selection only. Secrets belong in environment variables, never config files."""
    prices: str = "csv"
    corporate_actions: str = "csv"
    fundamentals: str = "csv"
    fx: str = "csv"

@dataclass
class PaperConfig:
    order_expiration_days: int = 1
    default_time_in_force: str = "DAY"
    allow_partial_fills: bool = False
    allow_zero_delisting: bool = True
    gtc_expiration_trading_days: int = 5
    quarantine_markdown_pct: float = 1.0
    corporate_actions_file: str | None = None

@dataclass
class Config:
    base_currency: str = "INR"
    initial_capital_inr: float = 100_000
    allocations: dict = field(default_factory=lambda: {"dividend_quality": .30, "dividend_growth": .20, "long_term_growth": .30, "cash_reserve": .20})
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    data_sources: DataSourceConfig = field(default_factory=DataSourceConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    tax: TaxConfig = field(default_factory=TaxConfig)
    def __post_init__(self):
        if not 0 < self.execution.max_volume_participation <= 1:
            raise ValueError('max_volume_participation must be in (0, 1]')
        if min(self.execution.india_cost_bps, self.execution.us_cost_bps, self.execution.slippage_bps) < 0:
            raise ValueError('execution costs and slippage cannot be negative')
        total = sum(float(v) for v in self.allocations.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"allocations must sum to 1.0, got {total}")
        if self.allocations.get("cash_reserve", 0) < self.risk.min_cash_weight:
            raise ValueError("cash_reserve must be at least min_cash_weight")
        if self.paper.default_time_in_force not in ("DAY", "GTC"):
            raise ValueError(f"default_time_in_force must be 'DAY' or 'GTC', got {self.paper.default_time_in_force}")
        if self.paper.gtc_expiration_trading_days <= 0:
            raise ValueError(f"gtc_expiration_trading_days must be positive, got {self.paper.gtc_expiration_trading_days}")
        if self.paper.order_expiration_days <= 0:
            raise ValueError(f"order_expiration_days must be positive, got {self.paper.order_expiration_days}")
        if not 0.0 <= self.paper.quarantine_markdown_pct <= 1.0:
            raise ValueError("quarantine_markdown_pct must be between 0.0 and 1.0")

def load_config(path: str = "config.yml") -> Config:
    p = Path(path)
    if not p.exists():
        return Config()
    raw = yaml.safe_load(p.read_text()) or {}
    return Config(**{
        **raw,
        "risk": RiskConfig(**raw.get("risk", {})),
        "execution": ExecutionConfig(**raw.get("execution", {})),
        "data_sources": DataSourceConfig(**raw.get("data_sources", {})),
        "paper": PaperConfig(**raw.get("paper", {})),
        "tax": TaxConfig(**raw.get("tax", {})),
    })
