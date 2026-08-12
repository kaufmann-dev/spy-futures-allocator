from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from app.auth import SqliteAuthStore
from app.db import (
    ContractRecord,
    Database,
    EventCaseRecord,
    EventStateRecord,
    MesQuoteRecord,
    Repository,
    SpyBarRecord,
)
from app.integrations.ibkr import IbkrMarketData, ResolvedContract, active_mes_expiry
from app.integrations.openai_events import (
    EventAssessment,
    EventCheckResult,
    EventType,
    ModelRun,
    ModelVerdict,
)
from app.security import SecretBox
from app.services import runtime as runtime_module
from app.services.jobs import build_scheduler
from app.services.runtime import ApplicationRuntime, RuntimeUnavailable


class FakeIbkr:
    def __init__(self, resolved: ResolvedContract, *, connected: bool = True) -> None:
        self.connected = connected
        self.resolved = resolved
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.resolve_dates: list[date | None] = []
        self.subscriptions: list[ResolvedContract] = []

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def resolve_active_mes(self, on_date: date | None = None) -> ResolvedContract:
        self.resolve_dates.append(on_date)
        return self.resolved

    def subscribe_mes(self, contract: ResolvedContract) -> None:
        self.subscriptions.append(contract)


def make_runtime(
    tmp_path: Path, *, now: datetime
) -> tuple[ApplicationRuntime, Repository, Callable[[], datetime]]:
    database_path = tmp_path / "runtime-safety.sqlite3"
    repository = Repository(Database(database_path))
    repository.initialize()

    def clock() -> datetime:
        return now

    service = ApplicationRuntime(
        repository,
        SqliteAuthStore(str(database_path)),
        SecretBox("settings-secret" * 4),
        clock=clock,
    )
    return service, repository, clock


def session_dates(service: ApplicationRuntime, *, end: date, count: int) -> list[date]:
    sessions: list[date] = []
    candidate = end
    while len(sessions) < count:
        if service.calendar.is_session(candidate):
            sessions.append(candidate)
        candidate -= timedelta(days=1)
    return list(reversed(sessions))


def append_bars(
    service: ApplicationRuntime,
    repository: Repository,
    sessions: list[date],
    closes: list[Decimal],
) -> None:
    for session, close in zip(sessions, closes, strict=True):
        completed_at = service.calendar.session_close(session).astimezone(UTC)
        repository.append_spy_bar(
            SpyBarRecord(
                session_date=session,
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1000"),
                completed_at=completed_at,
                fetched_at=completed_at + timedelta(minutes=1),
            )
        )


def resolved_mes(*, on_date: date, con_id: int = 123) -> ResolvedContract:
    expiry = active_mes_expiry(on_date)
    return ResolvedContract(
        con_id=con_id,
        symbol="MES",
        local_symbol=f"MES-{expiry}",
        security_type="FUT",
        exchange="CME",
        currency="USD",
        trading_class="MES",
        expiry=f"{expiry}19",
        multiplier=Decimal("5"),
    )


def configure_live_market(
    service: ApplicationRuntime,
    repository: Repository,
    *,
    now: datetime,
    contract: ResolvedContract | None = None,
) -> FakeIbkr:
    selected = contract or resolved_mes(on_date=now.astimezone().date())
    service.save_settings(
        strategy_amount_usd=Decimal("100000"),
        current_mes_quantity=None,
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=51,
        openai_api_key=None,
        detector_model="gpt-5.6-luna",
        verifier_model="gpt-5.6-terra",
    )
    repository.append_contract(
        ContractRecord(
            con_id=selected.con_id,
            symbol="MES",
            local_symbol=selected.local_symbol,
            trading_class=selected.trading_class,
            exchange=selected.exchange,
            currency=selected.currency,
            last_trade_date=selected.expiry,
            multiplier=selected.multiplier,
            resolved_at=now,
        )
    )
    repository.append_mes_quote(
        MesQuoteRecord(
            con_id=selected.con_id,
            bid=Decimal("5000"),
            ask=Decimal("5001"),
            last=Decimal("5000.5"),
            mark=Decimal("5000.5"),
            market_data_type=1,
            quoted_at=now,
            received_at=now,
        )
    )
    fake = FakeIbkr(selected)
    service._ibkr = cast(IbkrMarketData, fake)
    service._ibkr_key = ("127.0.0.1", 4002, 51)
    return fake


def model_run(*, role: str, verdict: ModelVerdict, response_hash: str) -> ModelRun:
    event_type = EventType.PANDEMIC
    return ModelRun(
        role=role,
        model=f"{role.lower()}-model",
        response_id=f"{role.lower()}-response",
        checked_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        assessment=EventAssessment(
            verdict=verdict,
            event_type=event_type,
            event_key="pathogen-x-2026",
            original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
            summary="Test evidence.",
            criteria=[],
            contradictions=[],
        ),
        sources=[],
        response_hash=response_hash,
    )


def event_result(
    *, state: str, detector: ModelRun, verifier: ModelRun | None = None
) -> EventCheckResult:
    return EventCheckResult(
        state=state,
        reason="test result",
        detector=detector,
        verifier=verifier,
        event_type=EventType.PANDEMIC,
        event_key="pathogen-x-2026",
        original_qualifying_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_clean_install_backfills_a_signal_snapshot_for_every_calculable_bar(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 12, 18, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    sessions = session_dates(service, end=date(2026, 8, 11), count=220)
    closes = [Decimal("400") + Decimal(index) / 10 for index in range(len(sessions))]
    append_bars(service, repository, sessions, closes)

    service.recalculate()

    snapshots = repository.signal_snapshot_history(1000)
    assert [snapshot.session_date for snapshot in snapshots] == sessions[199:]
    schedule = service.current_schedule(as_of=now)
    assert schedule.current_target_effective_at <= now


def test_event_overlay_replays_every_unprocessed_close_in_chronological_order(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 19, 12, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    sessions = session_dates(service, end=date(2026, 3, 18), count=24)
    closes = [Decimal("100")] * 20 + [
        Decimal("95"),
        Decimal("102"),
        Decimal("102"),
        Decimal("102"),
    ]
    append_bars(service, repository, sessions, closes)
    event_start = service.calendar.session_close(sessions[-4]).astimezone(UTC) - timedelta(
        minutes=1
    )
    case = repository.upsert_event_case(
        EventCaseRecord(
            event_key="war-2026",
            event_type="MAJOR_INTERSTATE_WAR",
            original_timestamp=event_start,
            window_start=event_start,
            window_end=event_start + timedelta(days=60),
            created_at=event_start,
            updated_at=event_start,
        )
    )
    assert case.id is not None
    repository.append_event_state(
        EventStateRecord(
            state="WINDOW_ACTIVE",
            event_cap=Decimal("4.85"),
            occurred_at=event_start,
            event_case_id=case.id,
        )
    )

    service.update_event_overlay()

    history = list(reversed(repository.event_state_history(case.id)))
    replayed = [item for item in history if item.last_evaluated_session is not None]
    assert [item.last_evaluated_session for item in replayed] == sessions[-4:]
    assert [item.state for item in replayed] == [
        "EVENT_LOCKED",
        "RECOVERY_TRACKING",
        "RECOVERY_TRACKING",
        "RECOVERY_CONFIRMED",
    ]
    count_after_replay = len(history)

    service.update_event_overlay()

    assert len(repository.event_state_history(case.id)) == count_after_replay


def test_overlay_is_applied_before_recalculation_can_publish_a_nonzero_target(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 19, 12, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    sessions = session_dates(service, end=date(2026, 3, 18), count=220)
    closes = [Decimal("100") + Decimal(index) / 5 for index in range(len(sessions))]
    closes[-1] = closes[-2] * Decimal("0.95")
    append_bars(service, repository, sessions, closes)
    configure_live_market(service, repository, now=now, contract=resolved_mes(on_date=now.date()))

    service.recalculate()
    baseline = repository.latest_recommendation()
    assert baseline is not None and baseline.status == "LIVE"
    assert baseline.final_target is not None and baseline.final_target > 0

    event_start = service.calendar.session_close(sessions[-1]).astimezone(UTC) - timedelta(
        minutes=1
    )
    case = repository.upsert_event_case(
        EventCaseRecord(
            event_key="war-2026",
            event_type="MAJOR_INTERSTATE_WAR",
            original_timestamp=event_start,
            window_start=event_start,
            window_end=event_start + timedelta(days=60),
            created_at=event_start,
            updated_at=event_start,
        )
    )
    assert case.id is not None
    repository.append_event_state(
        EventStateRecord(
            state="WINDOW_ACTIVE",
            event_cap=Decimal("4.85"),
            occurred_at=event_start,
            event_case_id=case.id,
        )
    )

    service.update_event_overlay()

    latest = repository.latest_recommendation()
    assert latest is not None and latest.status == "LIVE"
    assert latest.final_target == 0
    assert latest.quantity == 0
    history = repository.recommendation_history(3)
    assert [item.status for item in history] == ["LIVE", "UNAVAILABLE", "LIVE"]
    assert history[1].reason == "systemic event lock is being applied"


def test_live_quote_still_requires_a_live_ibkr_connection(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 18, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    contract = resolved_mes(on_date=now.date())
    configure_live_market(service, repository, now=now, contract=contract)
    service._ibkr = None

    with pytest.raises(RuntimeUnavailable, match="IBKR is disconnected"):
        service._persist_recommendation(
            now=now,
            target=Decimal("1"),
            final_target=Decimal("1"),
            effective_at=now,
            pending=False,
        )


def test_roll_cutoff_reconnects_resolves_and_subscribes_the_new_active_contract(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 10, 14, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    stale = resolved_mes(on_date=date(2026, 9, 9), con_id=111)
    active = resolved_mes(on_date=now.date(), con_id=222)
    repository.append_contract(
        ContractRecord(
            con_id=stale.con_id,
            symbol="MES",
            local_symbol=stale.local_symbol,
            trading_class=stale.trading_class,
            exchange=stale.exchange,
            currency=stale.currency,
            last_trade_date=stale.expiry,
            multiplier=stale.multiplier,
            resolved_at=now - timedelta(days=1),
        )
    )
    fake = FakeIbkr(active)
    service._ibkr = cast(IbkrMarketData, fake)
    settings = repository.get_settings()
    service._ibkr_key = (settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id)

    service.maintain_ibkr()

    assert fake.disconnect_calls == 1
    assert fake.connect_calls == 1
    assert fake.resolve_dates == [now.date()]
    assert fake.subscriptions == [active]
    assert repository.latest_contract() is not None
    assert repository.latest_contract().con_id == active.con_id  # type: ignore[union-attr]


def test_stored_contract_that_is_not_currently_active_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 14, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    stale = resolved_mes(on_date=date(2026, 9, 9), con_id=111)
    configure_live_market(service, repository, now=now, contract=stale)

    with pytest.raises(RuntimeUnavailable, match="active MES contract"):
        service._persist_recommendation(
            now=now,
            target=Decimal("1"),
            final_target=Decimal("1"),
            effective_at=now,
            pending=False,
        )


def test_market_refresh_job_persists_quotes_more_often_than_the_stale_limit(
    tmp_path: Path,
) -> None:
    scheduler = build_scheduler(tmp_path / "jobs.sqlite3")
    job = scheduler.get_job("refresh-ibkr-market-data")
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval < timedelta(seconds=60)


def test_human_review_history_does_not_block_later_confirmation(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 18, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    detector = model_run(
        role="DETECTOR", verdict=ModelVerdict.CANDIDATE, response_hash="same-evidence"
    )
    verifier = model_run(
        role="VERIFIER", verdict=ModelVerdict.CONFIRMED, response_hash="verified-evidence"
    )

    service._persist_event_check(event_result(state="NEEDS_HUMAN_REVIEW", detector=detector))
    service._persist_event_check(
        event_result(state="WINDOW_ACTIVE", detector=detector, verifier=verifier)
    )

    current = repository.latest_event_state()
    assert current is not None and current.state == "WINDOW_ACTIVE"
    assert current.event_case_id is not None
    assert {item.state for item in repository.event_state_history(current.event_case_id)} == {
        "NEEDS_HUMAN_REVIEW",
        "WINDOW_ACTIVE",
    }


def test_unchanged_detector_evidence_is_reverified_while_human_review_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 12, 18, tzinfo=UTC)
    service, repository, _ = make_runtime(tmp_path, now=now)
    detector = model_run(
        role="DETECTOR", verdict=ModelVerdict.CANDIDATE, response_hash="same-evidence"
    )
    verifier = model_run(
        role="VERIFIER", verdict=ModelVerdict.CONFIRMED, response_hash="verified-evidence"
    )
    service.save_settings(
        strategy_amount_usd=Decimal("100000"),
        current_mes_quantity=None,
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=51,
        openai_api_key="sk-project-test-value-with-enough-length",
        detector_model="gpt-5.6-luna",
        verifier_model="gpt-5.6-terra",
    )
    service._persist_event_check(event_result(state="NEEDS_HUMAN_REVIEW", detector=detector))
    verifier_calls = 0

    class FakeMonitor:
        def __init__(self, api_key: str, detector_model: str, verifier_model: str) -> None:
            del api_key, detector_model, verifier_model

        def detect(self) -> ModelRun:
            return detector

        def verify(self, candidate: ModelRun) -> ModelRun:
            nonlocal verifier_calls
            assert candidate is detector
            verifier_calls += 1
            return verifier

        def classify_detector(self, candidate: ModelRun) -> EventCheckResult | None:
            assert candidate is detector
            return None

        def classify_pair(self, first: ModelRun, second: ModelRun) -> EventCheckResult:
            assert first is detector and second is verifier
            return event_result(state="WINDOW_ACTIVE", detector=first, verifier=second)

    monkeypatch.setattr(runtime_module, "OpenAIEventMonitor", FakeMonitor)

    service.check_events()

    assert verifier_calls == 1
    current = repository.latest_event_state()
    assert current is not None and current.state == "WINDOW_ACTIVE"
