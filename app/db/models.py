"""Typed records persisted by the application.

Money, prices, and leverage values intentionally use :class:`Decimal`.  SQLite
stores them as text so that a database round-trip cannot introduce binary
floating-point rounding into a sizing recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AppSettings:
    strategy_amount_usd: Decimal | None
    current_mes_quantity: int | None
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    openai_api_key_ciphertext: bytes | None
    detector_model: str
    verifier_model: str
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
class MesQuoteRecord:
    con_id: int
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    mark: Decimal | None
    market_data_type: int | None
    quoted_at: datetime | None
    received_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class SignalSnapshotRecord:
    session_date: date
    spy_close: Decimal
    sma200: Decimal
    distance_above_sma200: Decimal
    daily_return: Decimal
    rv20: Decimal
    prior_20_session_high: Decimal
    dd20: Decimal
    base: Decimal
    bonus: Decimal
    volatility_cap: Decimal
    raw_target: Decimal
    shadow_raw_target: Decimal
    calculated_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTargetRecord:
    signal_session_date: date
    target: Decimal
    effective_at: datetime
    state: str
    calculated_at: datetime
    became_effective_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    status: str
    calculated_at: datetime
    is_pending: bool
    quantity: int | None
    contract_con_id: int | None
    contract_local_symbol: str | None
    strategy_amount_usd: Decimal | None
    mes_mark: Decimal | None
    mes_mark_at: datetime | None
    mes_multiplier: Decimal | None
    scheduled_target: Decimal | None
    event_cap: Decimal | None
    final_target: Decimal | None
    contract_notional: Decimal | None
    target_notional: Decimal | None
    achieved_notional: Decimal | None
    achieved_leverage: Decimal | None
    target_shortfall: Decimal | None
    target_effective_at: datetime | None
    reason: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class EventCaseRecord:
    event_key: str
    event_type: str
    original_timestamp: datetime
    window_start: datetime
    window_end: datetime
    created_at: datetime
    updated_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class EventModelRunRecord:
    role: str
    model: str
    result: str
    started_at: datetime
    completed_at: datetime
    event_case_id: int | None = None
    response_id: str | None = None
    structured_output_json: str | None = None
    error: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class EventDocumentRecord:
    url: str
    title: str
    publisher: str
    source_class: str
    retrieved_at: datetime
    content_hash: str
    event_case_id: int | None = None
    model_run_id: int | None = None
    published_at: datetime | None = None
    excerpt: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class EventEvidenceRecord:
    criterion_key: str
    supported: bool
    contradiction: bool
    recorded_at: datetime
    event_case_id: int | None = None
    document_id: int | None = None
    model_run_id: int | None = None
    excerpt: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class EventStateRecord:
    state: str
    event_cap: Decimal
    occurred_at: datetime
    event_case_id: int | None = None
    event_drawdown: Decimal | None = None
    lock_session: date | None = None
    post_exit_low: Decimal | None = None
    recovery_level: Decimal | None = None
    recovery_sessions: int = 0
    recovery_confirmed_at: datetime | None = None
    release_at: datetime | None = None
    last_evaluated_session: date | None = None
    reason: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    action: str
    occurred_at: datetime
    detail_json: str | None = None
    id: int | None = None
