"""Typed persistence records for SPY Futures Allocator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AppSettings:
    strategy_amount_usd: Decimal | None
    current_es_quantity: int | None
    current_mes_quantity: int | None
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    fast_sma_sessions: int
    slow_sma_sessions: int
    rebound_fraction: Decimal
    rv_window_sessions: int
    annualization_sessions: int
    rv_threshold: Decimal
    normal_exposure: Decimal
    high_vol_exposure: Decimal
    review_weekdays: tuple[int, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContractRecord:
    con_id: int
    symbol: str
    local_symbol: str
    exchange: str
    currency: str
    last_trade_date: str
    multiplier: Decimal
    resolved_at: datetime
    trading_class: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class SpyBarRecord:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    completed_at: datetime
    fetched_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class FuturesQuoteRecord:
    con_id: int
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    mark: Decimal | None
    market_data_type: int | None
    quoted_at: datetime | None
    received_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class StrategyConfigRecord:
    fingerprint: str
    config_json: str
    created_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class StrategySnapshotRecord:
    config_fingerprint: str
    session_date: date
    spy_close: Decimal
    fast_sma: Decimal
    slow_sma: Decimal
    rv: Decimal
    regime_state: str
    exit_armed: bool
    post_exit_low: Decimal | None
    reentry_level: Decimal | None
    volatility_state: str
    target_exposure: Decimal
    effective_at: datetime
    calculated_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    status: str
    calculated_at: datetime
    is_pending: bool
    config_fingerprint: str | None
    es_quantity: int | None
    mes_quantity: int | None
    es_contract_con_id: int | None
    es_contract_local_symbol: str | None
    es_mark: Decimal | None
    es_mark_at: datetime | None
    es_multiplier: Decimal | None
    mes_contract_con_id: int | None
    mes_contract_local_symbol: str | None
    mes_mark: Decimal | None
    mes_mark_at: datetime | None
    mes_multiplier: Decimal | None
    strategy_amount_usd: Decimal | None
    target_exposure: Decimal | None
    target_notional: Decimal | None
    achieved_notional: Decimal | None
    achieved_exposure: Decimal | None
    target_shortfall: Decimal | None
    target_effective_at: datetime | None
    reason: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    action: str
    occurred_at: datetime
    detail_json: str | None = None
    id: int | None = None
