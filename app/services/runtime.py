"""Runtime orchestration for the read-only SPY Futures Allocator."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from app.auth.store import AuthStore
from app.db import (
    AppSettings,
    AuditEntry,
    ContractRecord,
    FuturesQuoteRecord,
    RecommendationRecord,
    Repository,
    SpyBarRecord,
    StrategyConfigRecord,
    StrategySnapshotRecord,
)
from app.domain import (
    DailyBar,
    ScheduleState,
    StrategyConfig,
    TargetSignal,
    XnysCalendar,
    reconstruct_schedule,
    replay_strategy,
    schedule_target,
    size_futures,
)
from app.domain import next_review_close as domain_next_review_close
from app.domain import next_session_open as domain_next_session_open
from app.domain.errors import DomainError
from app.domain.schedule import MarketCalendar
from app.integrations.ibkr import (
    AdjustedSpyDailyBar,
    FuturesQuote,
    IbkrMarketData,
    IbkrUnavailable,
    ResolvedContract,
    active_futures_expiry,
)
from app.services.strategy_config import (
    serialize_strategy_config,
    strategy_config_fingerprint,
    strategy_config_from_settings,
)

logger = logging.getLogger(__name__)

QUOTE_MAX_AGE = timedelta(seconds=60)
SPY_FETCH_INTERVAL = timedelta(minutes=30)
HISTORY_LIMIT = 20_000
NEW_YORK = ZoneInfo("America/New_York")
EXPECTED_MULTIPLIERS = {"ES": Decimal("50"), "MES": Decimal("5")}


class RuntimeUnavailable(RuntimeError):
    """The runtime cannot safely publish a live allocation."""


class MarketDataClient(Protocol):
    """Read-only market-data operations consumed by the runtime."""

    @property
    def connected(self) -> bool: ...

    def connect(self, timeout: float = 10.0) -> None: ...

    def disconnect(self) -> None: ...

    def resolve_active_futures(
        self, on_date: date | None = None
    ) -> dict[str, ResolvedContract]: ...

    def subscribe_futures(
        self, contracts: Mapping[str, ResolvedContract] | None = None
    ) -> None: ...

    def wait_for_live_quotes(self, timeout: float = 15.0) -> dict[str, FuturesQuote]: ...

    def fetch_adjusted_spy_daily_bars(
        self, *, timeout: float = 45.0
    ) -> list[AdjustedSpyDailyBar]: ...


@dataclass(frozen=True, slots=True)
class _MarketInputs:
    es_contract: ContractRecord
    mes_contract: ContractRecord
    es_quote: FuturesQuoteRecord
    mes_quote: FuturesQuoteRecord


class ApplicationRuntime:
    def __init__(
        self,
        repository: Repository,
        auth_store: AuthStore,
        *,
        clock: Callable[[], datetime] | None = None,
        calendar: MarketCalendar | None = None,
        ibkr_factory: Callable[[str, int, int], MarketDataClient] | None = None,
    ) -> None:
        self.repository = repository
        self.auth_store = auth_store
        self.calendar = calendar or XnysCalendar()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ibkr_factory = ibkr_factory or IbkrMarketData
        self._ibkr: MarketDataClient | None = None
        self._ibkr_key: tuple[str, int, int] | None = None
        self._last_spy_fetch: datetime | None = None
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeUnavailable("runtime clock must include a timezone")
        return value.astimezone(UTC)

    @property
    def ibkr_state(self) -> str:
        return "LIVE" if self._ibkr is not None and self._ibkr.connected else "UNAVAILABLE"

    @property
    def configuration_fingerprint(self) -> str:
        return strategy_config_fingerprint(
            strategy_config_from_settings(self.repository.get_settings())
        )

    def bootstrap(self) -> None:
        self.refresh_market_data()

    def reset_ibkr(self) -> None:
        with self._lock:
            if self._ibkr is not None:
                self._ibkr.disconnect()
            self._ibkr = None
            self._ibkr_key = None
            self._last_spy_fetch = None

    def _client(self) -> MarketDataClient:
        settings = self.repository.get_settings()
        key = (settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id)
        if self._ibkr is None or self._ibkr_key != key:
            self.reset_ibkr()
            self._ibkr = self._ibkr_factory(*key)
            self._ibkr_key = key
        return self._ibkr

    @staticmethod
    def _expiry_month(expiry: str) -> str | None:
        month = expiry[:6]
        return month if len(month) == 6 and month.isdigit() else None

    @classmethod
    def _resolved_contracts_are_active(
        cls, contracts: Mapping[str, ResolvedContract], *, at: datetime
    ) -> bool:
        if set(contracts) != set(EXPECTED_MULTIPLIERS):
            return False
        expected_expiry = active_futures_expiry(at.astimezone(NEW_YORK).date())
        expiries: set[str] = set()
        for symbol, multiplier in EXPECTED_MULTIPLIERS.items():
            contract = contracts[symbol]
            if (
                contract.symbol != symbol
                or contract.security_type != "FUT"
                or contract.exchange != "CME"
                or contract.currency != "USD"
                or contract.multiplier != multiplier
                or contract.con_id <= 0
                or cls._expiry_month(contract.expiry) != expected_expiry
            ):
                return False
            expiries.add(contract.expiry)
        return len(expiries) == 1

    @classmethod
    def _stored_contracts_are_active(
        cls, contracts: Mapping[str, ContractRecord], *, at: datetime
    ) -> bool:
        if set(contracts) != set(EXPECTED_MULTIPLIERS):
            return False
        expected_expiry = active_futures_expiry(at.astimezone(NEW_YORK).date())
        expiries: set[str] = set()
        for symbol, multiplier in EXPECTED_MULTIPLIERS.items():
            contract = contracts[symbol]
            if (
                contract.symbol != symbol
                or contract.exchange != "CME"
                or contract.currency != "USD"
                or contract.multiplier != multiplier
                or contract.con_id <= 0
                or cls._expiry_month(contract.last_trade_date) != expected_expiry
            ):
                return False
            expiries.add(contract.last_trade_date)
        return len(expiries) == 1

    def _stored_contracts(self) -> dict[str, ContractRecord]:
        records: dict[str, ContractRecord] = {}
        for symbol in EXPECTED_MULTIPLIERS:
            record = self.repository.latest_contract(symbol)
            if record is not None:
                records[symbol] = record
        return records

    def maintain_ibkr(self) -> None:
        with self._lock:
            client = self._client()
            now = self._now()
            trading_date = now.astimezone(NEW_YORK).date()
            stored = self._stored_contracts()
            was_connected = client.connected
            if not was_connected:
                client.connect()

            stored_is_active = self._stored_contracts_are_active(stored, at=now)
            if was_connected and not stored_is_active:
                client.disconnect()
                client.connect()

            if not was_connected or not stored_is_active:
                resolved = client.resolve_active_futures(on_date=trading_date)
                if not self._resolved_contracts_are_active(resolved, at=now):
                    raise IbkrUnavailable(
                        "IBKR did not resolve active same-expiry ES and MES contracts"
                    )
                for symbol in EXPECTED_MULTIPLIERS:
                    contract = resolved[symbol]
                    self.repository.append_contract(
                        ContractRecord(
                            con_id=contract.con_id,
                            symbol=contract.symbol,
                            local_symbol=contract.local_symbol,
                            trading_class=contract.trading_class,
                            exchange=contract.exchange,
                            currency=contract.currency,
                            last_trade_date=contract.expiry,
                            multiplier=contract.multiplier,
                            resolved_at=now,
                        )
                    )
                client.subscribe_futures(resolved)
                self._audit(
                    "IBKR_CONNECTED" if not was_connected else "FUTURES_CONTRACTS_ROLLED",
                    {
                        "es_contract": resolved["ES"].local_symbol,
                        "mes_contract": resolved["MES"].local_symbol,
                        "expiry": resolved["ES"].expiry,
                    },
                    at=now,
                )

    @staticmethod
    def _require_aware(value: datetime, *, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeUnavailable(f"{field} must include a timezone")
        return value.astimezone(UTC)

    def _validate_live_quotes(
        self,
        quotes: Mapping[str, FuturesQuote],
        contracts: Mapping[str, ContractRecord],
        *,
        now: datetime,
    ) -> dict[str, FuturesQuoteRecord]:
        if set(quotes) != set(EXPECTED_MULTIPLIERS):
            raise RuntimeUnavailable("fresh live ES and MES quotes are required")
        if not self._stored_contracts_are_active(contracts, at=now):
            raise RuntimeUnavailable("active same-expiry ES and MES contracts are unavailable")

        records: dict[str, FuturesQuoteRecord] = {}
        for symbol in EXPECTED_MULTIPLIERS:
            quote = quotes[symbol]
            contract = contracts[symbol]
            quoted_at = self._require_aware(quote.timestamp, field=f"{symbol} quoted_at")
            if (
                quote.contract.con_id != contract.con_id
                or quote.contract.symbol != symbol
                or quote.contract.expiry != contract.last_trade_date
            ):
                raise RuntimeUnavailable(f"{symbol} quote does not match the active contract")
            if not quote.is_live:
                raise RuntimeUnavailable(f"{symbol} market data is not live")
            mark = quote.mark
            if mark is None or mark <= 0:
                raise RuntimeUnavailable(f"{symbol} mark is unavailable")
            if quoted_at > now or now - quoted_at > QUOTE_MAX_AGE:
                raise RuntimeUnavailable(f"{symbol} quote is stale")
            records[symbol] = FuturesQuoteRecord(
                con_id=contract.con_id,
                symbol=symbol,
                bid=quote.bid,
                ask=quote.ask,
                last=quote.last,
                mark=mark,
                market_data_type=quote.market_data_type,
                quoted_at=quoted_at,
                received_at=now,
            )
        return records

    def _latest_completed_session(self, now: datetime) -> date:
        local_date = now.astimezone(NEW_YORK).date()
        for offset in range(15):
            candidate = local_date - timedelta(days=offset)
            if not self.calendar.is_session(candidate):
                continue
            market_close = self._require_aware(
                self.calendar.session_close(candidate), field="market close"
            )
            if market_close <= now:
                return candidate
        raise RuntimeUnavailable("no completed XNYS session was found")

    def _require_contiguous_sessions(self, session_dates: list[date]) -> None:
        if not session_dates:
            raise RuntimeUnavailable("completed adjusted SPY history is unavailable")
        previous = session_dates[0]
        for current in session_dates[1:]:
            candidate = previous + timedelta(days=1)
            while candidate < current:
                if self.calendar.is_session(candidate):
                    raise RuntimeUnavailable(
                        f"adjusted SPY history is missing session {candidate.isoformat()}"
                    )
                candidate += timedelta(days=1)
            previous = current

    def _validated_fetched_bars(
        self, bars: list[AdjustedSpyDailyBar], *, now: datetime
    ) -> list[AdjustedSpyDailyBar]:
        deduped: dict[date, AdjustedSpyDailyBar] = {}
        for bar in bars:
            if not self.calendar.is_session(bar.session_date):
                continue
            completed_at = self._require_aware(
                self.calendar.session_close(bar.session_date), field="SPY session close"
            )
            if completed_at > now:
                continue
            if (
                min(bar.open, bar.high, bar.low, bar.close) <= 0
                or bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close)
                or bar.volume < 0
            ):
                raise RuntimeUnavailable("adjusted SPY history contains an invalid bar")
            deduped[bar.session_date] = bar

        ordered = [deduped[key] for key in sorted(deduped)]
        self._require_contiguous_sessions([bar.session_date for bar in ordered])
        required = strategy_config_from_settings(
            self.repository.get_settings()
        ).minimum_history_sessions
        if len(ordered) < required:
            raise RuntimeUnavailable(
                f"adjusted SPY history requires at least {required} completed sessions"
            )
        expected_latest = self._latest_completed_session(now)
        if ordered[-1].session_date != expected_latest:
            raise RuntimeUnavailable("adjusted SPY history is stale")
        return ordered

    def refresh_market_data(self) -> None:
        with self._lock:
            try:
                self._refresh_market_data_locked()
            except (DomainError, RuntimeUnavailable, IbkrUnavailable, ArithmeticError):
                self.recalculate()
                raise

    def _refresh_market_data_locked(self) -> None:
        self.maintain_ibkr()
        client = self._client()
        contracts = self._stored_contracts()
        try:
            quotes = client.wait_for_live_quotes()
        except IbkrUnavailable:
            self.reset_ibkr()
            raise
        now = self._now()
        quote_records = self._validate_live_quotes(quotes, contracts, now=now)
        for symbol in EXPECTED_MULTIPLIERS:
            self.repository.append_futures_quote(quote_records[symbol])

        latest_stored = self.repository.latest_spy_bars(1)
        history_is_current = bool(
            latest_stored
            and latest_stored[-1].session_date == self._latest_completed_session(now)
        )
        if (
            history_is_current
            and self._last_spy_fetch is not None
            and now - self._last_spy_fetch < SPY_FETCH_INTERVAL
        ):
            self.recalculate()
            return

        fetched = self._validated_fetched_bars(
            client.fetch_adjusted_spy_daily_bars(), now=now
        )
        existing = {
            record.session_date: record
            for record in self.repository.latest_spy_bars(HISTORY_LIMIT)
        }
        persisted = 0
        for bar in fetched:
            completed_at = self._require_aware(
                self.calendar.session_close(bar.session_date), field="SPY session close"
            )
            previous = existing.get(bar.session_date)
            values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            if previous is not None and values == (
                previous.open,
                previous.high,
                previous.low,
                previous.close,
                previous.volume,
            ):
                continue
            self.repository.upsert_spy_bar(
                SpyBarRecord(
                    session_date=bar.session_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    completed_at=completed_at,
                    fetched_at=now,
                )
            )
            persisted += 1
        self._last_spy_fetch = now
        if persisted:
            self._audit("SPY_BARS_IMPORTED", {"count": persisted}, at=now)
        self.recalculate()

    def _completed_spy_bars(self, now: datetime) -> list[SpyBarRecord]:
        records = self.repository.latest_spy_bars(HISTORY_LIMIT)
        completed: list[SpyBarRecord] = []
        for record in records:
            if not self.calendar.is_session(record.session_date):
                raise RuntimeUnavailable("stored SPY history contains a non-session bar")
            session_close = self._require_aware(
                self.calendar.session_close(record.session_date), field="SPY session close"
            )
            if record.completed_at < session_close:
                raise RuntimeUnavailable("stored SPY history contains an incomplete bar")
            if record.completed_at <= now:
                completed.append(record)
        if not completed:
            raise RuntimeUnavailable("completed adjusted SPY history is unavailable")
        self._require_contiguous_sessions([record.session_date for record in completed])
        if completed[-1].session_date != self._latest_completed_session(now):
            raise RuntimeUnavailable("adjusted SPY history is stale")
        return completed

    def _persist_replay(self, now: datetime) -> tuple[str, ScheduleState]:
        settings = self.repository.get_settings()
        config = strategy_config_from_settings(settings)
        fingerprint = strategy_config_fingerprint(config)
        self.repository.append_strategy_config(
            StrategyConfigRecord(
                fingerprint=fingerprint,
                config_json=serialize_strategy_config(config),
                created_at=now,
            )
        )

        bars = self._completed_spy_bars(now)
        replay = replay_strategy(
            [DailyBar(item.session_date, item.close) for item in bars], config
        )
        stored_snapshots: list[StrategySnapshotRecord] = []
        signals: list[TargetSignal] = []
        for snapshot in replay.snapshots:
            signal = TargetSignal(
                session_date=snapshot.session_date,
                target_exposure=snapshot.target_exposure,
                calculated_at=now,
            )
            effective_at = schedule_target(signal, self.calendar).effective_at
            stored_snapshots.append(
                StrategySnapshotRecord(
                    config_fingerprint=fingerprint,
                    session_date=snapshot.session_date,
                    spy_close=snapshot.metrics.spy_close,
                    fast_sma=snapshot.metrics.fast_sma,
                    slow_sma=snapshot.metrics.slow_sma,
                    rv=snapshot.metrics.rv,
                    regime_state=snapshot.regime.name,
                    exit_armed=snapshot.exit_armed,
                    post_exit_low=snapshot.post_exit_trough,
                    reentry_level=snapshot.reentry_level,
                    volatility_state=snapshot.volatility_state.name,
                    target_exposure=snapshot.target_exposure,
                    effective_at=effective_at,
                    calculated_at=now,
                )
            )
            if snapshot.target_changed:
                signals.append(signal)

        self.repository.replace_strategy_snapshots(fingerprint, stored_snapshots)
        schedule = reconstruct_schedule(signals, as_of=now, calendar=self.calendar)
        return fingerprint, schedule

    @staticmethod
    def _signals_from_records(records: list[StrategySnapshotRecord]) -> list[TargetSignal]:
        signals: list[TargetSignal] = []
        previous_target: Decimal | None = None
        for record in records:
            if previous_target is None or record.target_exposure != previous_target:
                signals.append(
                    TargetSignal(
                        session_date=record.session_date,
                        target_exposure=record.target_exposure,
                        calculated_at=record.calculated_at,
                    )
                )
            previous_target = record.target_exposure
        return signals

    def current_schedule(self, *, as_of: datetime | None = None) -> ScheduleState:
        selected_time = as_of or self._now()
        selected_time = self._require_aware(selected_time, field="as_of")
        fingerprint = self.configuration_fingerprint
        records = self.repository.strategy_snapshots(fingerprint, HISTORY_LIMIT)
        if not records:
            raise RuntimeUnavailable("strategy snapshots are unavailable")
        return reconstruct_schedule(
            self._signals_from_records(records),
            as_of=selected_time,
            calendar=self.calendar,
        )

    def next_market_open(self, *, as_of: datetime | None = None) -> datetime:
        return domain_next_session_open(as_of or self._now(), self.calendar)

    def next_regime_close(self, *, as_of: datetime | None = None) -> datetime:
        selected_time = self._require_aware(as_of or self._now(), field="as_of")
        first_date = selected_time.astimezone(NEW_YORK).date()
        for offset in range(371):
            candidate = first_date + timedelta(days=offset)
            if not self.calendar.is_session(candidate):
                continue
            market_close = self._require_aware(
                self.calendar.session_close(candidate), field="market close"
            )
            if market_close > selected_time:
                return market_close
        raise RuntimeUnavailable("no future XNYS regime close found within one year")

    def next_review_close(self, *, as_of: datetime | None = None) -> datetime:
        config = strategy_config_from_settings(self.repository.get_settings())
        return domain_next_review_close(as_of or self._now(), config, self.calendar)

    def _market_inputs(self, now: datetime) -> _MarketInputs:
        if self._ibkr is None or not self._ibkr.connected:
            raise RuntimeUnavailable("IBKR is disconnected")
        contracts = self._stored_contracts()
        if not self._stored_contracts_are_active(contracts, at=now):
            raise RuntimeUnavailable("active same-expiry ES and MES contracts are unavailable")
        es_contract = contracts["ES"]
        mes_contract = contracts["MES"]
        es_quote = self.repository.latest_futures_quote("ES")
        mes_quote = self.repository.latest_futures_quote("MES")
        if es_quote is None or mes_quote is None:
            raise RuntimeUnavailable("fresh live ES and MES quotes are unavailable")
        for symbol, quote, contract in (
            ("ES", es_quote, es_contract),
            ("MES", mes_quote, mes_contract),
        ):
            if quote.symbol != symbol or quote.con_id != contract.con_id:
                raise RuntimeUnavailable(f"{symbol} quote does not match the active contract")
            if quote.market_data_type != 1:
                raise RuntimeUnavailable(f"{symbol} market data is not live")
            if quote.mark is None or quote.mark <= 0 or quote.quoted_at is None:
                raise RuntimeUnavailable(f"{symbol} mark is unavailable")
            quoted_at = self._require_aware(quote.quoted_at, field=f"{symbol} quoted_at")
            received_at = self._require_aware(
                quote.received_at, field=f"{symbol} received_at"
            )
            if (
                quoted_at > now
                or received_at > now
                or now - quoted_at > QUOTE_MAX_AGE
                or now - received_at > QUOTE_MAX_AGE
            ):
                raise RuntimeUnavailable(f"{symbol} quote is stale")
        return _MarketInputs(es_contract, mes_contract, es_quote, mes_quote)

    def _recommendation(
        self,
        *,
        settings: AppSettings,
        fingerprint: str,
        market: _MarketInputs,
        target: Decimal,
        effective_at: datetime,
        pending: bool,
        now: datetime,
    ) -> RecommendationRecord:
        amount = settings.strategy_amount_usd
        if amount is None or not amount.is_finite() or amount <= 0:
            raise RuntimeUnavailable("strategy amount is not configured")
        es_mark = market.es_quote.mark
        mes_mark = market.mes_quote.mark
        es_mark_at = market.es_quote.quoted_at
        mes_mark_at = market.mes_quote.quoted_at
        if es_mark is None or mes_mark is None or es_mark_at is None or mes_mark_at is None:
            raise RuntimeUnavailable("fresh live ES and MES marks are unavailable")
        sizing = size_futures(
            strategy_amount_usd=amount,
            target_exposure=target,
            es_mark=es_mark,
            es_multiplier=market.es_contract.multiplier,
            mes_mark=mes_mark,
            mes_multiplier=market.mes_contract.multiplier,
            current_es_quantity=settings.current_es_quantity,
            current_mes_quantity=settings.current_mes_quantity,
        )
        return RecommendationRecord(
            status="LIVE",
            calculated_at=now,
            is_pending=pending,
            config_fingerprint=fingerprint,
            es_quantity=sizing.recommended_es,
            mes_quantity=sizing.recommended_mes,
            es_contract_con_id=market.es_contract.con_id,
            es_contract_local_symbol=market.es_contract.local_symbol,
            es_mark=es_mark,
            es_mark_at=es_mark_at,
            es_multiplier=market.es_contract.multiplier,
            mes_contract_con_id=market.mes_contract.con_id,
            mes_contract_local_symbol=market.mes_contract.local_symbol,
            mes_mark=mes_mark,
            mes_mark_at=mes_mark_at,
            mes_multiplier=market.mes_contract.multiplier,
            strategy_amount_usd=amount,
            target_exposure=target,
            target_notional=sizing.target_notional,
            achieved_notional=sizing.achieved_notional,
            achieved_exposure=sizing.achieved_exposure,
            target_shortfall=sizing.exposure_shortfall,
            target_effective_at=effective_at,
        )

    def _persist_live_recommendations(
        self, *, now: datetime, fingerprint: str, schedule: ScheduleState
    ) -> None:
        if schedule.pending_target is None:
            self._supersede_pending_recommendation(now=now, fingerprint=fingerprint)
        settings = self.repository.get_settings()
        market = self._market_inputs(now)
        current = self._recommendation(
            settings=settings,
            fingerprint=fingerprint,
            market=market,
            target=schedule.current_target,
            effective_at=schedule.current_target_effective_at,
            pending=False,
            now=now,
        )
        pending: RecommendationRecord | None = None
        if schedule.pending_target is not None and schedule.pending_target_effective_at is not None:
            pending = self._recommendation(
                settings=settings,
                fingerprint=fingerprint,
                market=market,
                target=schedule.pending_target,
                effective_at=schedule.pending_target_effective_at,
                pending=True,
                now=now,
            )
        self.repository.append_recommendation(current)
        if pending is not None:
            self.repository.append_recommendation(pending)

    def _supersede_pending_recommendation(
        self, *, now: datetime, fingerprint: str
    ) -> None:
        latest = self.repository.latest_recommendation(pending=True)
        if (
            latest is not None
            and latest.status == "UNAVAILABLE"
            and latest.reason == "no pending target"
            and latest.config_fingerprint == fingerprint
        ):
            return
        self.repository.append_recommendation(
            RecommendationRecord(
                status="UNAVAILABLE",
                calculated_at=now,
                is_pending=True,
                config_fingerprint=fingerprint,
                es_quantity=None,
                mes_quantity=None,
                es_contract_con_id=None,
                es_contract_local_symbol=None,
                es_mark=None,
                es_mark_at=None,
                es_multiplier=None,
                mes_contract_con_id=None,
                mes_contract_local_symbol=None,
                mes_mark=None,
                mes_mark_at=None,
                mes_multiplier=None,
                strategy_amount_usd=self.repository.get_settings().strategy_amount_usd,
                target_exposure=None,
                target_notional=None,
                achieved_notional=None,
                achieved_exposure=None,
                target_shortfall=None,
                target_effective_at=None,
                reason="no pending target",
            )
        )

    def recalculate(self) -> None:
        with self._lock:
            now = self._now()
            fingerprint: str | None = None
            schedule: ScheduleState | None = None
            try:
                fingerprint, schedule = self._persist_replay(now)
                self._persist_live_recommendations(
                    now=now, fingerprint=fingerprint, schedule=schedule
                )
            except (DomainError, RuntimeUnavailable, IbkrUnavailable, ArithmeticError) as exc:
                if fingerprint is None:
                    try:
                        fingerprint = self.configuration_fingerprint
                    except DomainError:
                        fingerprint = None
                self._persist_unavailable(
                    str(exc),
                    now=now,
                    fingerprint=fingerprint,
                    target=schedule.current_target if schedule is not None else None,
                    effective_at=(
                        schedule.current_target_effective_at if schedule is not None else None
                    ),
                )

    def _persist_unavailable(
        self,
        reason: str,
        *,
        now: datetime,
        fingerprint: str | None,
        target: Decimal | None,
        effective_at: datetime | None,
    ) -> None:
        latest = self.repository.latest_recommendation()
        strategy_amount = self.repository.get_settings().strategy_amount_usd
        if (
            latest is not None
            and latest.status == "UNAVAILABLE"
            and latest.reason == reason
            and latest.config_fingerprint == fingerprint
            and latest.strategy_amount_usd == strategy_amount
            and latest.target_exposure == target
            and latest.target_effective_at == effective_at
            and now - latest.calculated_at < timedelta(minutes=5)
        ):
            return
        self.repository.append_recommendation(
            RecommendationRecord(
                status="UNAVAILABLE",
                calculated_at=now,
                is_pending=False,
                config_fingerprint=fingerprint,
                es_quantity=None,
                mes_quantity=None,
                es_contract_con_id=None,
                es_contract_local_symbol=None,
                es_mark=None,
                es_mark_at=None,
                es_multiplier=None,
                mes_contract_con_id=None,
                mes_contract_local_symbol=None,
                mes_mark=None,
                mes_mark_at=None,
                mes_multiplier=None,
                strategy_amount_usd=strategy_amount,
                target_exposure=target,
                target_notional=None,
                achieved_notional=None,
                achieved_exposure=None,
                target_shortfall=None,
                target_effective_at=effective_at,
                reason=reason,
            )
        )

    @staticmethod
    def _validate_quantity(value: int | None, *, field: str) -> int | None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise DomainError(f"{field} must be an integer")
        return value

    def save_settings(
        self,
        *,
        strategy_amount_usd: Decimal,
        current_es_quantity: int | None,
        current_mes_quantity: int | None,
        ibkr_host: str,
        ibkr_port: int,
        ibkr_client_id: int,
        fast_sma_sessions: int,
        slow_sma_sessions: int,
        rebound_fraction: Decimal,
        rv_window_sessions: int,
        annualization_sessions: int,
        rv_threshold: Decimal,
        normal_exposure: Decimal,
        high_vol_exposure: Decimal,
        review_weekdays: tuple[int, ...],
    ) -> AppSettings:
        with self._lock:
            now = self._now()
            if not strategy_amount_usd.is_finite() or strategy_amount_usd <= 0:
                raise DomainError("strategy_amount_usd must be finite and positive")
            current_es = self._validate_quantity(
                current_es_quantity, field="current_es_quantity"
            )
            current_mes = self._validate_quantity(
                current_mes_quantity, field="current_mes_quantity"
            )
            host = ibkr_host.strip()
            if not host:
                raise DomainError("ibkr_host must not be empty")
            if isinstance(ibkr_port, bool) or not 1 <= ibkr_port <= 65_535:
                raise DomainError("ibkr_port must be between 1 and 65535")
            if isinstance(ibkr_client_id, bool) or ibkr_client_id < 0:
                raise DomainError("ibkr_client_id must be nonnegative")
            config = StrategyConfig(
                fast_sma_sessions=fast_sma_sessions,
                slow_sma_sessions=slow_sma_sessions,
                rebound_fraction=rebound_fraction,
                rv_window_sessions=rv_window_sessions,
                annualization_sessions=annualization_sessions,
                rv_threshold=rv_threshold,
                normal_exposure=normal_exposure,
                high_vol_exposure=high_vol_exposure,
                review_weekdays=review_weekdays,
            )
            old = self.repository.get_settings()
            updated = AppSettings(
                strategy_amount_usd=strategy_amount_usd,
                current_es_quantity=current_es,
                current_mes_quantity=current_mes,
                ibkr_host=host,
                ibkr_port=ibkr_port,
                ibkr_client_id=ibkr_client_id,
                fast_sma_sessions=config.fast_sma_sessions,
                slow_sma_sessions=config.slow_sma_sessions,
                rebound_fraction=config.rebound_fraction,
                rv_window_sessions=config.rv_window_sessions,
                annualization_sessions=config.annualization_sessions,
                rv_threshold=config.rv_threshold,
                normal_exposure=config.normal_exposure,
                high_vol_exposure=config.high_vol_exposure,
                review_weekdays=config.review_weekdays,
                updated_at=now,
            )
            self.repository.save_settings(updated)
            connection_changed = (
                old.ibkr_host,
                old.ibkr_port,
                old.ibkr_client_id,
            ) != (host, ibkr_port, ibkr_client_id)
            if connection_changed:
                self.reset_ibkr()
            self._audit(
                "SETTINGS_UPDATED",
                {
                    "strategy_amount_changed": old.strategy_amount_usd
                    != strategy_amount_usd,
                    "current_positions_changed": (
                        old.current_es_quantity,
                        old.current_mes_quantity,
                    )
                    != (current_es, current_mes),
                    "strategy_config_changed": strategy_config_fingerprint(
                        strategy_config_from_settings(old)
                    )
                    != strategy_config_fingerprint(config),
                    "ibkr_connection_changed": connection_changed,
                },
                at=now,
            )
            self.recalculate()
            return updated

    def reset_strategy(self) -> AppSettings:
        """Reset only strategy parameters and immediately replay stored history."""

        with self._lock:
            now = self._now()
            defaults = StrategyConfig()
            current = self.repository.get_settings()
            updated = replace(
                current,
                fast_sma_sessions=defaults.fast_sma_sessions,
                slow_sma_sessions=defaults.slow_sma_sessions,
                rebound_fraction=defaults.rebound_fraction,
                rv_window_sessions=defaults.rv_window_sessions,
                annualization_sessions=defaults.annualization_sessions,
                rv_threshold=defaults.rv_threshold,
                normal_exposure=defaults.normal_exposure,
                high_vol_exposure=defaults.high_vol_exposure,
                review_weekdays=defaults.review_weekdays,
                updated_at=now,
            )
            self.repository.save_settings(updated)
            self._audit("STRATEGY_RESET", {}, at=now)
            self.recalculate()
            return updated

    def prune_auth(self) -> None:
        self.auth_store.prune(int(self._now().timestamp()) - 7 * 24 * 60 * 60)

    def _audit(self, action: str, detail: dict[str, object], *, at: datetime) -> None:
        self.repository.append_audit(
            AuditEntry(
                action=action,
                detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
                occurred_at=at,
            )
        )
