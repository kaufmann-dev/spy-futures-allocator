from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import (
    AuditEntry,
    ContractRecord,
    Database,
    EventEvidenceRecord,
    EventModelRunRecord,
    EventStateRecord,
    MesQuoteRecord,
    RecommendationRecord,
    Repository,
    SignalSnapshotRecord,
    SpyBarRecord,
)

NOW = datetime(2026, 8, 12, 14, 30, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    repository = Repository(Database(tmp_path / "app.sqlite3"))
    repository.initialize()
    return repository


def test_initialize_is_idempotent_and_preserves_other_component_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    with database.connect() as connection:
        connection.execute("CREATE TABLE auth_owned (token TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO auth_owned VALUES ('kept')")
    database.initialize()
    database.initialize()

    with database.read() as connection:
        assert connection.execute("SELECT token FROM auth_owned").fetchone()["token"] == "kept"  # noqa: S105
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM schema_migrations WHERE component = 'app'"
            ).fetchone()["count"]
            == 1
        )


def test_decimal_market_records_round_trip_without_float_conversion(repository: Repository) -> None:
    repository.append_contract(
        ContractRecord(
            con_id=123,
            symbol="MES",
            local_symbol="MESU6",
            exchange="CME",
            currency="USD",
            last_trade_date="20260918",
            multiplier=Decimal("5.0000"),
            resolved_at=NOW,
        )
    )
    repository.append_spy_bar(
        SpyBarRecord(
            session_date=date(2026, 8, 11),
            open=Decimal("701.1012"),
            high=Decimal("706.3311"),
            low=Decimal("700.0001"),
            close=Decimal("705.1234"),
            volume=Decimal("12345678"),
            completed_at=NOW,
            fetched_at=NOW,
        )
    )
    repository.append_mes_quote(
        MesQuoteRecord(
            con_id=123,
            bid=Decimal("7012.25"),
            ask=Decimal("7012.50"),
            last=Decimal("7012.25"),
            mark=Decimal("7012.375"),
            market_data_type=1,
            quoted_at=NOW,
            received_at=NOW,
        )
    )

    contract = repository.latest_contract()
    bar = repository.latest_spy_bar()
    quote = repository.latest_mes_quote()
    assert contract is not None and contract.multiplier == Decimal("5.0000")
    assert bar is not None and bar.close == Decimal("705.1234")
    assert quote is not None and quote.mark == Decimal("7012.375")


def test_latest_spy_bars_uses_latest_revision_per_session(repository: Repository) -> None:
    for fetched_at, close in [
        (datetime(2026, 8, 12, 1, tzinfo=UTC), Decimal("700")),
        (datetime(2026, 8, 12, 2, tzinfo=UTC), Decimal("701")),
    ]:
        repository.append_spy_bar(
            SpyBarRecord(
                session_date=date(2026, 8, 11),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=close,
                volume=None,
                completed_at=fetched_at,
                fetched_at=fetched_at,
            )
        )

    bars = repository.latest_spy_bars()
    assert len(bars) == 1
    assert bars[0].close == Decimal("701")


def test_signal_history_and_lookup_are_bounded_and_chronological(repository: Repository) -> None:
    for offset in range(3):
        repository.append_signal_snapshot(
            SignalSnapshotRecord(
                session_date=date(2026, 8, 8 + offset),
                spy_close=Decimal("700"),
                sma200=Decimal("600"),
                distance_above_sma200=Decimal("0.1"),
                daily_return=Decimal("0.01"),
                rv20=Decimal("0.12"),
                prior_20_session_high=Decimal("705"),
                dd20=Decimal("-0.007"),
                base=Decimal("4.85"),
                bonus=Decimal("0.6"),
                volatility_cap=Decimal("8.3333"),
                raw_target=Decimal(str(offset)),
                shadow_raw_target=Decimal(str(offset)),
                calculated_at=datetime(2026, 8, 8 + offset, 22, tzinfo=UTC),
            )
        )

    assert [item.raw_target for item in repository.signal_snapshot_history(2)] == [
        Decimal("1"),
        Decimal("2"),
    ]
    assert len(repository.signal_snapshots_for_session(date(2026, 8, 9))) == 1
    with pytest.raises(ValueError):
        repository.signal_snapshot_history(0)


def test_incomplete_evidence_can_exist_without_event_case(repository: Repository) -> None:
    model_run = repository.append_event_model_run(
        EventModelRunRecord(
            role="DETECTOR",
            model="detector-model",
            result="INCONCLUSIVE",
            started_at=NOW,
            completed_at=NOW,
        )
    )
    evidence = repository.append_event_evidence(
        EventEvidenceRecord(
            event_case_id=None,
            model_run_id=model_run.id,
            criterion_key="original_timestamp",
            supported=False,
            contradiction=False,
            excerpt="No qualifying timestamp found.",
            recorded_at=NOW,
        )
    )

    assert evidence.id is not None
    assert model_run.id is not None
    assert repository.event_evidence_for_model_run(model_run.id)[0].event_case_id is None


def test_event_recovery_state_round_trips_all_restart_fields(repository: Repository) -> None:
    record = repository.append_event_state(
        EventStateRecord(
            state="RECOVERY_CONFIRMED",
            event_cap=Decimal("0"),
            occurred_at=NOW,
            event_drawdown=Decimal("-0.01"),
            lock_session=date(2026, 8, 1),
            post_exit_low=Decimal("650.12"),
            recovery_level=Decimal("695.6284"),
            recovery_sessions=3,
            recovery_confirmed_at=NOW,
            release_at=datetime(2026, 8, 14, 13, 30, tzinfo=UTC),
            last_evaluated_session=date(2026, 8, 11),
        )
    )

    loaded = repository.latest_event_state()
    assert loaded == record


def test_recommendation_and_audit_history_are_bounded(repository: Repository) -> None:
    for offset in range(3):
        repository.append_recommendation(
            RecommendationRecord(
                status="LIVE",
                calculated_at=datetime(2026, 8, 12, 12, offset, tzinfo=UTC),
                is_pending=False,
                quantity=offset,
                contract_con_id=None,
                contract_local_symbol="MESU6",
                strategy_amount_usd=Decimal("100000"),
                mes_mark=Decimal("7000"),
                mes_mark_at=NOW,
                mes_multiplier=Decimal("5"),
                scheduled_target=Decimal("1"),
                event_cap=Decimal("4.85"),
                final_target=Decimal("1"),
                contract_notional=Decimal("35000"),
                target_notional=Decimal("100000"),
                achieved_notional=Decimal(str(offset * 35000)),
                achieved_leverage=Decimal(str(offset)) * Decimal("0.35"),
                target_shortfall=Decimal("1") - Decimal(str(offset)) * Decimal("0.35"),
                target_effective_at=NOW,
            )
        )
        repository.append_audit(AuditEntry(action=f"event-{offset}", occurred_at=NOW))

    assert [item.quantity for item in repository.recommendation_history(2)] == [2, 1]
    assert len(repository.audit_history(2)) == 2
