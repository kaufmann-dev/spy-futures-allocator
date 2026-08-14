from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from app.auth.store import AuthStore
from app.db import Database, Repository, SpyBarRecord
from app.domain.errors import DomainError
from app.integrations.ibkr import (
    AdjustedSpyDailyBar,
    FuturesQuote,
    IbkrUnavailable,
    ResolvedContract,
)
from app.services.runtime import (
    ApplicationRuntime,
    MarketDataClient,
    RuntimeUnavailable,
)

NOW = datetime(2026, 8, 14, 21, tzinfo=UTC)
NY = ZoneInfo("America/New_York")


class FakeCalendar:
    def is_session(self, session_date: date) -> bool:
        return session_date.weekday() < 5

    def session_open(self, session_date: date) -> datetime:
        return datetime.combine(session_date, time(9, 30), NY)

    def session_close(self, session_date: date) -> datetime:
        return datetime.combine(session_date, time(16), NY)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass
class RecordingAuthStore:
    pruned_before: int | None = None

    def prune(self, before: int) -> None:
        self.pruned_before = before


def resolved_contracts(*, mes_expiry: str = "20260918") -> dict[str, ResolvedContract]:
    return {
        "ES": ResolvedContract(
            con_id=101,
            symbol="ES",
            local_symbol="ESU6",
            security_type="FUT",
            exchange="CME",
            currency="USD",
            trading_class="ES",
            expiry="20260918",
            multiplier=Decimal("50"),
        ),
        "MES": ResolvedContract(
            con_id=202,
            symbol="MES",
            local_symbol="MESU6",
            security_type="FUT",
            exchange="CME",
            currency="USD",
            trading_class="MES",
            expiry=mes_expiry,
            multiplier=Decimal("5"),
        ),
    }


def futures_quotes(
    contracts: Mapping[str, ResolvedContract],
    *,
    quoted_at: datetime,
    mark: str = "5000",
    market_data_type: int = 1,
) -> dict[str, FuturesQuote]:
    price = Decimal(mark)
    return {
        symbol: FuturesQuote(
            contract=contracts[symbol],
            bid=price - Decimal("0.25"),
            ask=price + Decimal("0.25"),
            last=price,
            timestamp=quoted_at,
            market_data_type=market_data_type,
        )
        for symbol in ("ES", "MES")
    }


def weekday_sessions(*, ending: date, count: int) -> list[date]:
    sessions: list[date] = []
    candidate = ending
    while len(sessions) < count:
        if candidate.weekday() < 5:
            sessions.append(candidate)
        candidate -= timedelta(days=1)
    return list(reversed(sessions))


def adjusted_bars(
    *, ending: date = date(2026, 8, 14), closes: list[str] | None = None, count: int = 505
) -> list[AdjustedSpyDailyBar]:
    close_values = closes or ["100"] * count
    sessions = weekday_sessions(ending=ending, count=len(close_values))
    return [
        AdjustedSpyDailyBar(
            session_date=session,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal("1000000"),
        )
        for session, close in zip(sessions, close_values, strict=True)
    ]


class FakeMarketData:
    def __init__(
        self,
        *,
        bars: list[AdjustedSpyDailyBar],
        contracts: dict[str, ResolvedContract] | None = None,
        quotes: dict[str, FuturesQuote] | None = None,
    ) -> None:
        self.connected = False
        self.contracts = contracts or resolved_contracts()
        self.quotes = quotes or futures_quotes(self.contracts, quoted_at=NOW)
        self.bars = bars
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.resolve_calls = 0
        self.subscribe_calls = 0
        self.quote_calls = 0
        self.fetch_calls = 0
        self.on_quote_wait: Callable[[], None] | None = None
        self.quote_error: IbkrUnavailable | None = None
        self.connection_key: tuple[str, int, int] | None = None

    def connect(self, timeout: float = 10.0) -> None:  # noqa: ARG002
        self.connect_calls += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def resolve_active_futures(
        self, on_date: date | None = None  # noqa: ARG002
    ) -> dict[str, ResolvedContract]:
        self.resolve_calls += 1
        return dict(self.contracts)

    def subscribe_futures(
        self, contracts: Mapping[str, ResolvedContract] | None = None  # noqa: ARG002
    ) -> None:
        self.subscribe_calls += 1

    def wait_for_live_quotes(self, timeout: float = 15.0) -> dict[str, FuturesQuote]:  # noqa: ARG002
        self.quote_calls += 1
        if self.on_quote_wait is not None:
            self.on_quote_wait()
        if self.quote_error is not None:
            raise self.quote_error
        return dict(self.quotes)

    def fetch_adjusted_spy_daily_bars(
        self, *, timeout: float = 45.0  # noqa: ARG002
    ) -> list[AdjustedSpyDailyBar]:
        self.fetch_calls += 1
        return list(self.bars)


@dataclass(frozen=True)
class Harness:
    service: ApplicationRuntime
    repository: Repository
    market_data: FakeMarketData
    clock: MutableClock
    auth_store: RecordingAuthStore


def build_harness(
    tmp_path: Path,
    *,
    bars: list[AdjustedSpyDailyBar] | None = None,
    amount: Decimal | None = Decimal("1000000"),
    settings_changes: dict[str, object] | None = None,
    contracts: dict[str, ResolvedContract] | None = None,
    quotes: dict[str, FuturesQuote] | None = None,
    now: datetime = NOW,
) -> Harness:
    repository = Repository(Database(tmp_path / "runtime.sqlite3"))
    repository.initialize()
    clock = MutableClock(now)
    settings = replace(
        repository.get_settings(),
        strategy_amount_usd=amount,
        updated_at=now,
        **cast(Any, settings_changes or {}),
    )
    repository.save_settings(settings)
    market_data = FakeMarketData(
        bars=bars or adjusted_bars(), contracts=contracts, quotes=quotes
    )
    auth_store = RecordingAuthStore()

    def factory(host: str, port: int, client_id: int) -> MarketDataClient:
        market_data.connection_key = (host, port, client_id)
        return market_data

    service = ApplicationRuntime(
        repository,
        cast(AuthStore, auth_store),
        clock=clock,
        calendar=FakeCalendar(),
        ibkr_factory=factory,
    )
    return Harness(service, repository, market_data, clock, auth_store)


def test_refresh_persists_dual_live_quotes_adjusted_history_replay_and_allocation(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)

    harness.service.refresh_market_data()

    assert harness.market_data.connect_calls == 1
    assert harness.market_data.resolve_calls == 1
    assert harness.market_data.subscribe_calls == 1
    assert harness.market_data.fetch_calls == 1
    assert len(harness.repository.latest_spy_bars()) == 505
    assert harness.repository.latest_futures_quote("ES") is not None
    assert harness.repository.latest_futures_quote("MES") is not None
    fingerprint = harness.service.configuration_fingerprint
    assert harness.repository.strategy_config(fingerprint) is not None
    assert len(harness.repository.strategy_snapshots(fingerprint)) == 6

    recommendation = harness.repository.latest_recommendation()
    assert recommendation is not None
    assert recommendation.status == "LIVE"
    assert recommendation.config_fingerprint == fingerprint
    assert recommendation.target_exposure == Decimal("1.60")
    assert recommendation.es_quantity == 6
    assert recommendation.mes_quantity == 4
    assert recommendation.es_mark == Decimal("5000.00")
    assert recommendation.mes_mark == Decimal("5000.00")
    assert harness.repository.latest_recommendation(pending=True) is not None
    assert harness.repository.latest_recommendation(pending=True).status == "UNAVAILABLE"  # type: ignore[union-attr]


def test_refresh_captures_now_after_waiting_for_a_new_quote(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    arrived_at = NOW + timedelta(seconds=2)

    def arrive() -> None:
        harness.clock.value = arrived_at
        harness.market_data.quotes = futures_quotes(
            harness.market_data.contracts, quoted_at=arrived_at
        )

    harness.market_data.on_quote_wait = arrive

    harness.service.refresh_market_data()

    recommendation = harness.repository.latest_recommendation()
    assert recommendation is not None
    assert recommendation.status == "LIVE"
    assert recommendation.calculated_at == arrived_at
    assert recommendation.es_mark_at == arrived_at


def test_quote_refresh_immediately_resizes_without_refetching_history(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.service.refresh_market_data()
    harness.clock.value += timedelta(seconds=30)
    harness.market_data.quotes = futures_quotes(
        harness.market_data.contracts,
        quoted_at=harness.clock.value,
        mark="5100",
    )

    harness.service.refresh_market_data()

    assert harness.market_data.fetch_calls == 1
    recommendation = harness.repository.latest_recommendation()
    assert recommendation is not None
    assert recommendation.status == "LIVE"
    assert recommendation.es_mark == Decimal("5100.00")
    assert recommendation.mes_mark == Decimal("5100.00")
    assert recommendation.es_quantity == 6
    assert recommendation.mes_quantity == 2


def test_history_throttle_is_bypassed_when_a_new_session_just_closed(tmp_path: Path) -> None:
    before_close = datetime(2026, 8, 14, 19, 59, tzinfo=UTC)
    harness = build_harness(
        tmp_path,
        now=before_close,
        bars=adjusted_bars(ending=date(2026, 8, 13)),
        quotes=futures_quotes(resolved_contracts(), quoted_at=before_close),
    )
    harness.service.refresh_market_data()
    harness.clock.value = datetime(2026, 8, 14, 20, 1, tzinfo=UTC)
    harness.market_data.quotes = futures_quotes(
        harness.market_data.contracts, quoted_at=harness.clock.value
    )
    harness.market_data.bars = adjusted_bars(ending=date(2026, 8, 14), count=506)

    harness.service.refresh_market_data()

    assert harness.market_data.fetch_calls == 2
    assert harness.repository.latest_spy_bars(1)[-1].session_date == date(2026, 8, 14)


def test_same_expiry_and_official_multipliers_are_required(tmp_path: Path) -> None:
    invalid = resolved_contracts(mes_expiry="20261218")
    harness = build_harness(tmp_path, contracts=invalid)

    with pytest.raises(IbkrUnavailable, match="same-expiry"):
        harness.service.maintain_ibkr()

    assert harness.repository.latest_contract("ES") is None
    assert harness.repository.latest_contract("MES") is None


def test_delayed_or_stale_quotes_never_persist_or_allocate(tmp_path: Path) -> None:
    contracts = resolved_contracts()
    delayed = futures_quotes(contracts, quoted_at=NOW, market_data_type=3)
    harness = build_harness(tmp_path, contracts=contracts, quotes=delayed)

    with pytest.raises(RuntimeUnavailable, match="ES market data is not live"):
        harness.service.refresh_market_data()

    assert harness.repository.latest_futures_quote("ES") is None
    latest = harness.repository.latest_recommendation()
    assert latest is not None
    assert latest.status == "UNAVAILABLE"


def test_quote_wait_failure_resets_connection_so_next_cycle_resubscribes(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    harness.service.maintain_ibkr()
    harness.market_data.quote_error = IbkrUnavailable("partial subscription failed")

    with pytest.raises(IbkrUnavailable, match="partial subscription"):
        harness.service.refresh_market_data()

    assert harness.service.ibkr_state == "UNAVAILABLE"
    assert harness.market_data.disconnect_calls == 1
    harness.market_data.quote_error = None
    harness.service.refresh_market_data()
    assert harness.market_data.resolve_calls == 2
    assert harness.market_data.subscribe_calls == 2
    assert harness.repository.latest_recommendation().status == "LIVE"  # type: ignore[union-attr]


def test_recalculation_fails_closed_when_saved_quotes_age_out(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.service.refresh_market_data()
    harness.clock.value += timedelta(seconds=61)

    harness.service.recalculate()

    result = harness.repository.latest_recommendation()
    assert result is not None
    assert result.status == "UNAVAILABLE"
    assert result.target_exposure == Decimal("1.60")
    assert result.reason == "ES quote is stale"


def test_fetched_history_with_a_missing_session_fails_closed(tmp_path: Path) -> None:
    history = adjusted_bars()
    del history[-20]
    harness = build_harness(tmp_path, bars=history)

    with pytest.raises(RuntimeUnavailable, match="missing session"):
        harness.service.refresh_market_data()

    result = harness.repository.latest_recommendation()
    assert result is not None
    assert result.status == "UNAVAILABLE"


def test_stored_history_with_a_missing_session_fails_closed(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        settings_changes={
            "fast_sma_sessions": 2,
            "slow_sma_sessions": 3,
            "rv_window_sessions": 2,
            "annualization_sessions": 4,
        },
    )
    sessions = [date(2026, 8, 11), date(2026, 8, 13), date(2026, 8, 14)]
    for session in sessions:
        close_at = FakeCalendar().session_close(session).astimezone(UTC)
        harness.repository.upsert_spy_bar(
            SpyBarRecord(
                session_date=session,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=Decimal("1"),
                completed_at=close_at,
                fetched_at=NOW,
            )
        )

    harness.service.recalculate()

    result = harness.repository.latest_recommendation()
    assert result is not None
    assert result.status == "UNAVAILABLE"
    assert result.reason is not None and "missing session 2026-08-12" in result.reason


def pending_bars(*, high_on_friday: bool) -> list[AdjustedSpyDailyBar]:
    closes = ["100", "100", "100", "120" if high_on_friday else "100"]
    return adjusted_bars(closes=closes)


def pending_settings() -> dict[str, object]:
    return {
        "fast_sma_sessions": 2,
        "slow_sma_sessions": 3,
        "rv_window_sessions": 2,
        "annualization_sessions": 4,
        "rv_threshold": Decimal("0.10"),
        "review_weekdays": (4,),
    }


def test_only_a_changed_close_target_creates_a_pending_allocation(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path, bars=pending_bars(high_on_friday=True), settings_changes=pending_settings()
    )

    harness.service.refresh_market_data()

    current = harness.repository.latest_recommendation()
    pending = harness.repository.latest_recommendation(pending=True)
    assert current is not None and current.status == "LIVE"
    assert current.target_exposure == Decimal("1.60")
    assert pending is not None and pending.status == "LIVE"
    assert pending.target_exposure == Decimal("0.60")
    assert pending.target_effective_at == datetime(2026, 8, 17, 9, 30, tzinfo=NY)
    assert pending.es_quantity == 2
    assert pending.mes_quantity == 4
    schedule = harness.service.current_schedule()
    assert schedule.current_target == Decimal("1.60")
    assert schedule.pending_target == Decimal("0.60")


def test_corrected_replay_without_pending_target_supersedes_old_pending(
    tmp_path: Path,
) -> None:
    harness = build_harness(
        tmp_path, bars=pending_bars(high_on_friday=True), settings_changes=pending_settings()
    )
    harness.service.refresh_market_data()
    assert harness.repository.latest_recommendation(pending=True).status == "LIVE"  # type: ignore[union-attr]
    harness.clock.value += timedelta(minutes=31)
    harness.market_data.quotes = futures_quotes(
        harness.market_data.contracts, quoted_at=harness.clock.value
    )
    harness.market_data.bars = pending_bars(high_on_friday=False)

    harness.service.refresh_market_data()

    pending = harness.repository.latest_recommendation(pending=True)
    assert pending is not None
    assert pending.status == "UNAVAILABLE"
    assert pending.reason == "no pending target"
    assert harness.service.current_schedule().pending_target is None


def test_save_and_reset_replay_immediately_and_preserve_manual_positions(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    harness.service.refresh_market_data()

    saved = harness.service.save_settings(
        strategy_amount_usd=Decimal("1000000"),
        current_es_quantity=-1,
        current_mes_quantity=5,
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=51,
        fast_sma_sessions=100,
        slow_sma_sessions=500,
        rebound_fraction=Decimal("0.25"),
        rv_window_sessions=20,
        annualization_sessions=252,
        rv_threshold=Decimal("0.25"),
        normal_exposure=Decimal("2.00"),
        high_vol_exposure=Decimal("0.75"),
        review_weekdays=(0, 3),
    )

    assert saved.current_es_quantity == -1
    assert saved.current_mes_quantity == 5
    assert harness.repository.latest_recommendation().target_exposure == Decimal("2.00")  # type: ignore[union-attr]
    custom_fingerprint = harness.service.configuration_fingerprint
    assert harness.repository.strategy_snapshots(custom_fingerprint)

    reset = harness.service.reset_strategy()

    assert reset.strategy_amount_usd == Decimal("1000000")
    assert reset.current_es_quantity == -1
    assert reset.current_mes_quantity == 5
    assert (reset.ibkr_host, reset.ibkr_port, reset.ibkr_client_id) == (
        "127.0.0.1",
        4002,
        51,
    )
    assert reset.normal_exposure == Decimal("1.60")
    assert reset.high_vol_exposure == Decimal("0.60")
    assert harness.service.configuration_fingerprint != custom_fingerprint
    assert harness.repository.latest_recommendation().target_exposure == Decimal("1.60")  # type: ignore[union-attr]


def test_unavailable_dedup_does_not_hide_a_new_target_within_five_minutes(
    tmp_path: Path,
) -> None:
    settings = pending_settings() | {"review_weekdays": (0, 1, 2, 3, 4)}
    harness = build_harness(
        tmp_path,
        bars=pending_bars(high_on_friday=False),
        settings_changes=settings,
    )
    for bar in harness.market_data.bars:
        close_at = FakeCalendar().session_close(bar.session_date).astimezone(UTC)
        harness.repository.upsert_spy_bar(
            SpyBarRecord(
                session_date=bar.session_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                completed_at=close_at,
                fetched_at=NOW,
            )
        )
    harness.service.recalculate()
    first = harness.repository.latest_recommendation()
    assert first is not None and first.target_exposure == Decimal("1.60")

    changed = adjusted_bars(closes=["100", "100", "120", "120"])
    for bar in changed:
        close_at = FakeCalendar().session_close(bar.session_date).astimezone(UTC)
        harness.repository.upsert_spy_bar(
            SpyBarRecord(
                session_date=bar.session_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                completed_at=close_at,
                fetched_at=NOW,
            )
        )
    harness.service.recalculate()

    history = harness.repository.recommendation_history()
    assert len(history) == 2
    assert history[0].status == "UNAVAILABLE"
    assert history[0].target_exposure == Decimal("0.60")
    assert history[1].target_exposure == Decimal("1.60")


def test_operational_schedule_methods_use_the_injected_clock(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.service.refresh_market_data()

    assert harness.service.next_market_open().astimezone(UTC) == datetime(
        2026, 8, 17, 13, 30, tzinfo=UTC
    )
    assert harness.service.next_regime_close().astimezone(UTC) == datetime(
        2026, 8, 17, 20, tzinfo=UTC
    )
    assert harness.service.next_review_close().astimezone(UTC) == datetime(
        2026, 8, 17, 20, tzinfo=UTC
    )
    assert harness.service.current_schedule().next_market_open.astimezone(UTC) == datetime(
        2026, 8, 17, 13, 30, tzinfo=UTC
    )


def test_bootstrap_publishes_one_recommendation_and_prune_uses_injected_time(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)

    harness.service.bootstrap()
    harness.service.prune_auth()

    assert len(harness.repository.recommendation_history()) == 1
    assert harness.auth_store.pruned_before == int(NOW.timestamp()) - 7 * 24 * 60 * 60


def test_invalid_strategy_settings_are_rejected_before_persistence(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    original = harness.repository.get_settings()

    with pytest.raises(DomainError, match="less than slow"):
        harness.service.save_settings(
            strategy_amount_usd=Decimal("1000000"),
            current_es_quantity=None,
            current_mes_quantity=None,
            ibkr_host="127.0.0.1",
            ibkr_port=4002,
            ibkr_client_id=51,
            fast_sma_sessions=500,
            slow_sma_sessions=500,
            rebound_fraction=Decimal("0.25"),
            rv_window_sessions=20,
            annualization_sessions=252,
            rv_threshold=Decimal("0.25"),
            normal_exposure=Decimal("1.60"),
            high_vol_exposure=Decimal("0.60"),
            review_weekdays=(0, 3),
        )

    assert harness.repository.get_settings() == original
