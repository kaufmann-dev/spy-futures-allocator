from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.auth.store import SqliteAuthStore
from app.db import (
    AppSettings,
    AuditEntry,
    ContractRecord,
    EventCaseRecord,
    EventDocumentRecord,
    EventEvidenceRecord,
    EventModelRunRecord,
    EventStateRecord,
    MesQuoteRecord,
    RecommendationRecord,
    Repository,
    ScheduledTargetRecord,
    SignalSnapshotRecord,
    SpyBarRecord,
)
from app.domain import (
    DailyBar,
    EventOverlay,
    EventState,
    EventType,
    RawTargetSnapshot,
    ScheduleState,
    XnysCalendar,
    apply_event_cap,
    calculate_leverage,
    event_cap,
    reconstruct_schedule,
    release_confirmed_recovery,
    size_mes,
    update_event_after_close,
)
from app.domain.errors import DomainError
from app.integrations.ibkr import IbkrMarketData, IbkrUnavailable, ResolvedContract
from app.integrations.openai_events import EventCheckResult, ModelRun, OpenAIEventMonitor
from app.security import SecretBox

logger = logging.getLogger(__name__)

OPENAI_KEY_PURPOSE = "arl5-simplified:openai-api-key"
QUOTE_MAX_AGE = timedelta(seconds=60)
NEW_YORK = ZoneInfo("America/New_York")


class RuntimeUnavailable(RuntimeError):
    pass


class ApplicationRuntime:
    def __init__(
        self,
        repository: Repository,
        auth_store: SqliteAuthStore,
        secret_box: SecretBox,
    ) -> None:
        self.repository = repository
        self.auth_store = auth_store
        self.secret_box = secret_box
        self.calendar = XnysCalendar()
        self._ibkr: IbkrMarketData | None = None
        self._ibkr_key: tuple[str, int, int] | None = None
        self._lock = threading.RLock()
        self._last_spy_fetch: datetime | None = None

    @property
    def ibkr_state(self) -> str:
        return "LIVE" if self._ibkr is not None and self._ibkr.connected else "UNAVAILABLE"

    def bootstrap(self) -> None:
        try:
            self.refresh_market_data()
        finally:
            self.recalculate()
            self.update_event_overlay()

    def reset_ibkr(self) -> None:
        with self._lock:
            if self._ibkr is not None:
                self._ibkr.disconnect()
            self._ibkr = None
            self._ibkr_key = None
            self._last_spy_fetch = None

    def _client(self) -> IbkrMarketData:
        settings = self.repository.get_settings()
        key = (settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id)
        if self._ibkr is None or self._ibkr_key != key:
            self.reset_ibkr()
            self._ibkr = IbkrMarketData(*key)
            self._ibkr_key = key
        return self._ibkr

    def maintain_ibkr(self) -> None:
        with self._lock:
            client = self._client()
            if not client.connected:
                client.connect()
                resolved = client.resolve_active_mes()
                self._persist_contract(resolved)
                client.subscribe_mes(resolved)
                self._audit("IBKR_CONNECTED", {"contract": resolved.local_symbol})

    def _persist_contract(self, contract: ResolvedContract) -> None:
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
                resolved_at=datetime.now(UTC),
            )
        )

    def refresh_market_data(self) -> None:
        with self._lock:
            self.maintain_ibkr()
            client = self._client()
            now = datetime.now(UTC)
            try:
                quote = client.current_mes_quote()
            except IbkrUnavailable:
                quote = None
            if quote is not None:
                self.repository.append_mes_quote(
                    MesQuoteRecord(
                        con_id=quote.contract.con_id,
                        bid=quote.bid,
                        ask=quote.ask,
                        last=quote.last,
                        mark=quote.mark,
                        market_data_type=quote.market_data_type,
                        quoted_at=quote.timestamp,
                        received_at=now,
                    )
                )

            if self._last_spy_fetch is not None and now - self._last_spy_fetch < timedelta(
                minutes=30
            ):
                return
            bars = client.fetch_spy_daily_bars()
            existing = {item.session_date: item for item in self.repository.latest_spy_bars(1000)}
            persisted = 0
            for bar in bars:
                if not self.calendar.is_session(bar.session_date):
                    continue
                completed_at = self.calendar.session_close(bar.session_date).astimezone(UTC)
                if completed_at > now:
                    continue
                previous = existing.get(bar.session_date)
                if previous and (
                    previous.open,
                    previous.high,
                    previous.low,
                    previous.close,
                    previous.volume,
                ) == (bar.open, bar.high, bar.low, bar.close, bar.volume):
                    continue
                self.repository.append_spy_bar(
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
                self._audit("SPY_BARS_IMPORTED", {"count": persisted})

    def _raw_snapshots(self) -> list[RawTargetSnapshot]:
        # Keep only the latest calculation per session when a corrected IBKR bar
        # produced a later immutable snapshot.
        latest: dict[date, SignalSnapshotRecord] = {}
        for item in self.repository.signal_snapshot_history(2000):
            latest[item.session_date] = item
        return [
            RawTargetSnapshot(item.session_date, item.raw_target, item.calculated_at)
            for item in sorted(latest.values(), key=lambda value: value.session_date)
        ]

    def current_schedule(self, *, as_of: datetime | None = None) -> ScheduleState:
        return reconstruct_schedule(
            self._raw_snapshots(), as_of=as_of or datetime.now(UTC), calendar=self.calendar
        )

    def _calculate_latest_signal(self, now: datetime) -> SignalSnapshotRecord:
        bars = self.repository.latest_spy_bars(1000)
        daily = [DailyBar(item.session_date, item.close, completed=True) for item in bars]
        result = calculate_leverage(daily)
        latest_bar = bars[-1]
        existing = self.repository.signal_snapshots_for_session(latest_bar.session_date)
        if existing:
            current = existing[0]
            if current.spy_close == latest_bar.close:
                return current
        snapshot = SignalSnapshotRecord(
            session_date=latest_bar.session_date,
            spy_close=result.metrics.spy_close,
            sma200=result.metrics.sma200,
            distance_above_sma200=result.metrics.distance_above_sma200,
            daily_return=result.metrics.daily_return,
            rv20=result.metrics.rv20,
            prior_20_session_high=result.metrics.prior_20_session_high,
            dd20=result.metrics.dd20,
            base=result.base,
            bonus=result.bonus,
            volatility_cap=result.volatility_cap,
            raw_target=result.raw_target,
            shadow_raw_target=result.raw_target,
            calculated_at=max(now, latest_bar.completed_at),
        )
        return self.repository.append_signal_snapshot(snapshot)

    def _persist_schedule(self, schedule: object, now: datetime) -> None:
        for target, effective_at, state in (
            (
                schedule.current_scheduled_target,  # type: ignore[attr-defined]
                schedule.current_target_effective_at,  # type: ignore[attr-defined]
                "EFFECTIVE",
            ),
            (
                schedule.pending_scheduled_target,  # type: ignore[attr-defined]
                schedule.pending_target_effective_at,  # type: ignore[attr-defined]
                "PENDING",
            ),
        ):
            if target is None or effective_at is None:
                continue
            execution_date = effective_at.astimezone(NEW_YORK).date()
            signal_date = execution_date - timedelta(days=1)
            existing = self.repository.scheduled_targets_for_signal(signal_date)
            if any(
                item.target == target and item.effective_at == effective_at and item.state == state
                for item in existing
            ):
                continue
            self.repository.append_scheduled_target(
                ScheduledTargetRecord(
                    signal_session_date=signal_date,
                    target=target,
                    effective_at=effective_at,
                    state=state,
                    calculated_at=now,
                    became_effective_at=now if state == "EFFECTIVE" else None,
                )
            )

    def recalculate(self) -> None:
        with self._lock:
            now = datetime.now(UTC)
            try:
                self._calculate_latest_signal(now)
                schedule = reconstruct_schedule(
                    self._raw_snapshots(), as_of=now, calendar=self.calendar
                )
                self._persist_schedule(schedule, now)
                self._release_recovery_if_due(now)
                state_record = self.repository.latest_event_state()
                state = EventState(state_record.state) if state_record else EventState.NO_EVENT
                final_target = apply_event_cap(schedule.current_scheduled_target, state)
                self._persist_recommendation(
                    now=now,
                    target=schedule.current_scheduled_target,
                    final_target=final_target,
                    effective_at=schedule.current_target_effective_at,
                    pending=False,
                )
                if (
                    schedule.pending_scheduled_target is not None
                    and schedule.pending_target_effective_at is not None
                ):
                    pending_final = apply_event_cap(schedule.pending_scheduled_target, state)
                    self._persist_recommendation(
                        now=now,
                        target=schedule.pending_scheduled_target,
                        final_target=pending_final,
                        effective_at=schedule.pending_target_effective_at,
                        pending=True,
                    )
            except (DomainError, RuntimeUnavailable, IbkrUnavailable, ValueError) as exc:
                self._persist_unavailable(str(exc), now=now)

    def _persist_recommendation(
        self,
        *,
        now: datetime,
        target: Decimal,
        final_target: Decimal,
        effective_at: datetime,
        pending: bool,
    ) -> None:
        settings = self.repository.get_settings()
        if settings.strategy_amount_usd is None or settings.strategy_amount_usd <= 0:
            raise RuntimeUnavailable("strategy amount is not configured")
        quote = self.repository.latest_mes_quote()
        contract = self.repository.latest_contract()
        if quote is None or contract is None or quote.con_id != contract.con_id:
            raise RuntimeUnavailable("live MES contract and quote are unavailable")
        if quote.market_data_type != 1:
            raise RuntimeUnavailable("MES market data is not live")
        if quote.mark is None or quote.quoted_at is None:
            raise RuntimeUnavailable("MES mark is unavailable")
        if now - quote.received_at > QUOTE_MAX_AGE or now - quote.quoted_at > QUOTE_MAX_AGE:
            raise RuntimeUnavailable("MES quote is stale")
        sizing = size_mes(
            strategy_amount_usd=settings.strategy_amount_usd,
            final_target=final_target,
            mes_mark=quote.mark,
            mes_multiplier=contract.multiplier,
            current_mes_quantity=settings.current_mes_quantity,
        )
        current_event_state = self.repository.latest_event_state()
        current_event_cap = (
            event_cap(EventState(current_event_state.state))
            if current_event_state is not None
            else Decimal("4.85")
        )
        self.repository.append_recommendation(
            RecommendationRecord(
                status="LIVE",
                calculated_at=now,
                is_pending=pending,
                quantity=sizing.recommended_mes,
                contract_con_id=contract.con_id,
                contract_local_symbol=contract.local_symbol,
                strategy_amount_usd=settings.strategy_amount_usd,
                mes_mark=quote.mark,
                mes_mark_at=quote.quoted_at,
                mes_multiplier=contract.multiplier,
                scheduled_target=target,
                event_cap=current_event_cap,
                final_target=final_target,
                contract_notional=sizing.mes_contract_notional,
                target_notional=sizing.target_notional,
                achieved_notional=sizing.achieved_notional,
                achieved_leverage=sizing.achieved_leverage,
                target_shortfall=sizing.target_shortfall,
                target_effective_at=effective_at,
            )
        )

    def _persist_unavailable(self, reason: str, *, now: datetime) -> None:
        latest = self.repository.latest_recommendation()
        if (
            latest is not None
            and latest.status == "UNAVAILABLE"
            and latest.reason == reason
            and now - latest.calculated_at < timedelta(minutes=5)
        ):
            return
        self.repository.append_recommendation(
            RecommendationRecord(
                status="UNAVAILABLE",
                calculated_at=now,
                is_pending=False,
                quantity=None,
                contract_con_id=None,
                contract_local_symbol=None,
                strategy_amount_usd=self.repository.get_settings().strategy_amount_usd,
                mes_mark=None,
                mes_mark_at=None,
                mes_multiplier=None,
                scheduled_target=None,
                event_cap=None,
                final_target=None,
                contract_notional=None,
                target_notional=None,
                achieved_notional=None,
                achieved_leverage=None,
                target_shortfall=None,
                target_effective_at=None,
                reason=reason,
            )
        )

    def _overlay(self, state: EventStateRecord, case: EventCaseRecord) -> EventOverlay:
        return EventOverlay(
            event_id=case.event_key,
            event_type=EventType(case.event_type),
            state=EventState(state.state),
            window_start=case.window_start,
            window_end=case.window_end,
            current_event_drawdown=state.event_drawdown,
            lock_session=state.lock_session,
            post_exit_low=state.post_exit_low,
            recovery_level=state.recovery_level,
            consecutive_recovery_closes=state.recovery_sessions,
            recovery_confirmed_at=state.recovery_confirmed_at,
            release_at=state.release_at,
            last_evaluated_session=state.last_evaluated_session,
        )

    def _persist_overlay(self, overlay: EventOverlay, case_id: int, at: datetime) -> None:
        self.repository.append_event_state(
            EventStateRecord(
                state=overlay.state.value,
                event_cap=event_cap(overlay.state),
                occurred_at=at,
                event_case_id=case_id,
                event_drawdown=overlay.current_event_drawdown,
                lock_session=overlay.lock_session,
                post_exit_low=overlay.post_exit_low,
                recovery_level=overlay.recovery_level,
                recovery_sessions=overlay.consecutive_recovery_closes,
                recovery_confirmed_at=overlay.recovery_confirmed_at,
                release_at=overlay.release_at,
                last_evaluated_session=overlay.last_evaluated_session,
            )
        )

    def _release_recovery_if_due(self, now: datetime) -> None:
        state = self.repository.latest_event_state()
        if state is None or state.event_case_id is None or state.state != "RECOVERY_CONFIRMED":
            return
        case = self.repository.event_case(state.event_case_id)
        if case is None:
            raise RuntimeUnavailable("event state references a missing case")
        released = release_confirmed_recovery(self._overlay(state, case), at=now)
        if released.state != EventState(state.state):
            self._persist_overlay(released, state.event_case_id, now)

    def update_event_overlay(self) -> None:
        with self._lock:
            now = datetime.now(UTC)
            state = self.repository.latest_event_state()
            if state is None or state.event_case_id is None:
                return
            if state.state not in {
                "WINDOW_ACTIVE",
                "EVENT_LOCKED",
                "RECOVERY_TRACKING",
                "RECOVERY_CONFIRMED",
            }:
                return
            case = self.repository.event_case(state.event_case_id)
            if case is None:
                raise RuntimeUnavailable("event state references a missing case")
            bars = self.repository.latest_spy_bars(1000)
            latest = bars[-1]
            if state.last_evaluated_session == latest.session_date:
                self._release_recovery_if_due(now)
                return
            closes = [bar.close for bar in bars]
            if len(closes) < 21:
                raise RuntimeUnavailable("event drawdown requires 21 completed SPY closes")
            updated = update_event_after_close(
                self._overlay(state, case),
                bar=DailyBar(latest.session_date, latest.close),
                prior_20_session_high=max(closes[-21:-1]),
                completed_at=latest.completed_at,
                calendar=self.calendar,
            )
            self._persist_overlay(updated, state.event_case_id, now)

    def _api_key(self, settings: AppSettings) -> str:
        encrypted = settings.openai_api_key_ciphertext
        if encrypted is None:
            raise RuntimeUnavailable("OpenAI API key is not configured")
        envelope = bytes(encrypted).decode()
        return self.secret_box.decrypt(envelope, purpose=OPENAI_KEY_PURPOSE)

    def check_events(self) -> None:
        with self._lock:
            settings = self.repository.get_settings()
            monitor = OpenAIEventMonitor(
                self._api_key(settings), settings.detector_model, settings.verifier_model
            )
            detector = monitor.detect()
            previous = self.repository.latest_event_model_run("DETECTOR")
            if previous is not None and previous.structured_output_json:
                try:
                    previous_payload = json.loads(previous.structured_output_json)
                except json.JSONDecodeError:
                    previous_payload = {}
                if (
                    isinstance(previous_payload, dict)
                    and previous_payload.get("evidence_hash") == detector.response_hash
                ):
                    self._audit("EVENT_EVIDENCE_UNCHANGED", {"detector": detector.model})
                    return
            initial = monitor.classify_detector(detector)
            if initial is not None:
                result = initial
            else:
                verifier = monitor.verify(detector)
                result = monitor.classify_pair(detector, verifier)
            self._persist_event_check(result)

    def _persist_event_check(self, result: EventCheckResult) -> None:
        now = datetime.now(UTC)
        current = self.repository.latest_event_state()
        case: EventCaseRecord | None = None
        if (
            result.event_type.value != "NONE"
            and result.original_qualifying_timestamp is not None
            and result.event_key
        ):
            existing = self.repository.event_case_by_key(result.event_key)
            case = existing or self.repository.upsert_event_case(
                EventCaseRecord(
                    event_key=result.event_key,
                    event_type=result.event_type.value,
                    original_timestamp=result.original_qualifying_timestamp,
                    window_start=result.original_qualifying_timestamp,
                    window_end=result.original_qualifying_timestamp + timedelta(days=60),
                    created_at=now,
                    updated_at=now,
                )
            )
        self._persist_model_run(result.detector, case)
        if result.verifier is not None:
            self._persist_model_run(result.verifier, case)

        existing_active = current is not None and current.state in {
            "WINDOW_ACTIVE",
            "EVENT_LOCKED",
            "RECOVERY_TRACKING",
            "RECOVERY_CONFIRMED",
        }
        if result.state == "WINDOW_ACTIVE" and case is not None and case.id is not None:
            if existing_active or self.repository.event_state_history(case.id):
                self._audit("EVENT_DUPLICATE_IGNORED", {"event_key": result.event_key})
                return
            self.repository.append_event_state(
                EventStateRecord(
                    state="WINDOW_ACTIVE",
                    event_cap=Decimal("4.85"),
                    occurred_at=now,
                    event_case_id=case.id,
                    reason=result.reason,
                )
            )
            self._audit("EVENT_WINDOW_ACTIVATED", {"event_key": result.event_key})
        elif not existing_active:
            self.repository.append_event_state(
                EventStateRecord(
                    state=result.state,
                    event_cap=Decimal("4.85"),
                    occurred_at=now,
                    event_case_id=case.id if case else None,
                    reason=result.reason,
                )
            )

    def _persist_model_run(self, run: ModelRun, case: EventCaseRecord | None) -> None:
        case_id = case.id if case else None
        stored_run = self.repository.append_event_model_run(
            EventModelRunRecord(
                event_case_id=case_id,
                role=run.role,
                model=run.model,
                response_id=run.response_id,
                result=run.assessment.verdict.value,
                structured_output_json=json.dumps(
                    {
                        "assessment": run.assessment.model_dump(mode="json"),
                        "evidence_hash": run.response_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                error=None,
                started_at=run.checked_at,
                completed_at=run.checked_at,
            )
        )
        documents: dict[str, EventDocumentRecord] = {}
        for source in run.sources:
            publisher = urlparse(source.url).hostname or "unknown"
            stored = self.repository.append_event_document(
                EventDocumentRecord(
                    event_case_id=case_id,
                    model_run_id=stored_run.id,
                    url=source.url,
                    title=source.title,
                    publisher=publisher,
                    source_class=source.source_class,
                    retrieved_at=run.checked_at,
                    content_hash=hashlib.sha256(source.url.encode()).hexdigest(),
                )
            )
            documents[source.url] = stored
        for item in run.assessment.criteria:
            first = next((documents[url] for url in item.citation_urls if url in documents), None)
            self.repository.append_event_evidence(
                EventEvidenceRecord(
                    event_case_id=case_id,
                    document_id=first.id if first else None,
                    model_run_id=stored_run.id,
                    criterion_key=item.criterion,
                    supported=item.supported,
                    contradiction=False,
                    excerpt=item.explanation,
                    recorded_at=run.checked_at,
                )
            )
        for contradiction in run.assessment.contradictions:
            self.repository.append_event_evidence(
                EventEvidenceRecord(
                    event_case_id=case_id,
                    model_run_id=stored_run.id,
                    criterion_key="CONTRADICTION",
                    supported=False,
                    contradiction=True,
                    excerpt=contradiction,
                    recorded_at=run.checked_at,
                )
            )

    def prune_auth(self) -> None:
        self.auth_store.prune(int(datetime.now(UTC).timestamp()) - 7 * 24 * 60 * 60)

    def save_settings(
        self,
        *,
        strategy_amount_usd: Decimal,
        current_mes_quantity: int | None,
        ibkr_host: str,
        ibkr_port: int,
        ibkr_client_id: int,
        openai_api_key: str | None,
        detector_model: str,
        verifier_model: str,
    ) -> AppSettings:
        old = self.repository.get_settings()
        ciphertext = old.openai_api_key_ciphertext
        if openai_api_key:
            ciphertext = self.secret_box.encrypt(
                openai_api_key, purpose=OPENAI_KEY_PURPOSE
            ).encode()
        updated = self.repository.update_settings(
            strategy_amount_usd=strategy_amount_usd,
            current_mes_quantity=current_mes_quantity,
            ibkr_host=ibkr_host,
            ibkr_port=ibkr_port,
            ibkr_client_id=ibkr_client_id,
            openai_api_key_ciphertext=ciphertext,
            detector_model=detector_model,
            verifier_model=verifier_model,
        )
        if (old.ibkr_host, old.ibkr_port, old.ibkr_client_id) != (
            ibkr_host,
            ibkr_port,
            ibkr_client_id,
        ):
            self.reset_ibkr()
        self._audit(
            "SETTINGS_UPDATED",
            {
                "strategy_amount_changed": old.strategy_amount_usd != strategy_amount_usd,
                "ibkr_connection_changed": self._ibkr_key is None,
                "openai_key_replaced": bool(openai_api_key),
            },
        )
        return updated

    def _audit(self, action: str, detail: dict[str, object]) -> None:
        self.repository.append_audit(
            AuditEntry(
                action=action,
                detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
                occurred_at=datetime.now(UTC),
            )
        )
