from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import (
    AuditEntry,
    ContractRecord,
    Database,
    FuturesQuoteRecord,
    RecommendationRecord,
    Repository,
    SpyBarRecord,
    StrategyConfigRecord,
    StrategySnapshotRecord,
)

NOW = datetime(2026, 8, 14, 20, tzinfo=UTC)


def repository(tmp_path: Path) -> Repository:
    result = Repository(Database(tmp_path / "allocator.sqlite3"))
    result.initialize()
    return result


def test_settings_defaults_and_update_round_trip(tmp_path: Path) -> None:
    store = repository(tmp_path)
    defaults = store.get_settings()
    assert defaults.strategy_amount_usd is None
    assert defaults.current_es_quantity is None
    assert defaults.current_mes_quantity is None
    assert defaults.fast_sma_sessions == 100
    assert defaults.slow_sma_sessions == 500
    assert defaults.rebound_fraction == Decimal("0.25")
    assert defaults.rv_window_sessions == 20
    assert defaults.annualization_sessions == 252
    assert defaults.rv_threshold == Decimal("0.25")
    assert defaults.normal_exposure == Decimal("1.60")
    assert defaults.high_vol_exposure == Decimal("0.60")
    assert defaults.review_weekdays == (0, 3)

    updated = store.update_settings(
        strategy_amount_usd=Decimal("125000"),
        current_es_quantity=-1,
        current_mes_quantity=7,
        fast_sma_sessions=80,
        slow_sma_sessions=420,
        review_weekdays=(1, 4),
    )
    assert updated.strategy_amount_usd == Decimal("125000")
    assert updated.current_es_quantity == -1
    assert updated.current_mes_quantity == 7
    assert store.get_settings().review_weekdays == (1, 4)


def test_fresh_schema_refuses_a_legacy_database(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE app_settings (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="fresh database path"):
        Database(path).initialize()


def test_adjusted_spy_bar_upsert_replaces_revised_history(tmp_path: Path) -> None:
    store = repository(tmp_path)
    first = SpyBarRecord(
        session_date=date(2026, 8, 13),
        open=Decimal("640"),
        high=Decimal("645"),
        low=Decimal("639"),
        close=Decimal("644"),
        volume=Decimal("10"),
        completed_at=NOW - timedelta(days=1),
        fetched_at=NOW,
    )
    store.upsert_spy_bar(first)
    store.upsert_spy_bar(
        SpyBarRecord(
            session_date=first.session_date,
            open=first.open,
            high=first.high,
            low=first.low,
            close=Decimal("643.50"),
            volume=first.volume,
            completed_at=first.completed_at,
            fetched_at=NOW + timedelta(minutes=30),
        )
    )
    bars = store.latest_spy_bars()
    assert len(bars) == 1
    assert bars[0].close == Decimal("643.50")


def test_contracts_and_quotes_are_partitioned_by_symbol(tmp_path: Path) -> None:
    store = repository(tmp_path)
    for symbol, multiplier, con_id in (("ES", "50", 1), ("MES", "5", 2)):
        store.append_contract(
            ContractRecord(
                con_id=con_id,
                symbol=symbol,
                local_symbol=f"{symbol}Z6",
                trading_class=symbol,
                exchange="CME",
                currency="USD",
                last_trade_date="20261218",
                multiplier=Decimal(multiplier),
                resolved_at=NOW,
            )
        )
        store.append_futures_quote(
            FuturesQuoteRecord(
                con_id=con_id,
                symbol=symbol,
                bid=Decimal("6500"),
                ask=Decimal("6501"),
                last=Decimal("6500.25"),
                mark=Decimal("6500.5"),
                market_data_type=1,
                quoted_at=NOW,
                received_at=NOW,
            )
        )
    assert store.latest_contract("ES").multiplier == Decimal("50")  # type: ignore[union-attr]
    assert store.latest_contract("MES").multiplier == Decimal("5")  # type: ignore[union-attr]
    assert store.latest_futures_quote("ES").symbol == "ES"  # type: ignore[union-attr]
    assert store.latest_futures_quote("MES").symbol == "MES"  # type: ignore[union-attr]


def snapshot(
    fingerprint: str, day: date, exposure: str, effective_at: datetime
) -> StrategySnapshotRecord:
    return StrategySnapshotRecord(
        config_fingerprint=fingerprint,
        session_date=day,
        spy_close=Decimal("640"),
        fast_sma=Decimal("630"),
        slow_sma=Decimal("620"),
        rv=Decimal("0.18"),
        regime_state="RISK_ON",
        exit_armed=True,
        post_exit_low=None,
        reentry_level=None,
        volatility_state="NORMAL",
        target_exposure=Decimal(exposure),
        effective_at=effective_at,
        calculated_at=NOW,
    )


def test_configuration_snapshots_replace_atomically_and_schedule_by_effective_time(
    tmp_path: Path,
) -> None:
    store = repository(tmp_path)
    fingerprint = "a" * 64
    stored = store.append_strategy_config(
        StrategyConfigRecord(fingerprint, '{"normal_exposure":"1.60"}', NOW)
    )
    assert stored.id is not None
    current_time = NOW + timedelta(hours=1)
    records = [
        snapshot(fingerprint, date(2026, 8, 13), "1.60", NOW),
        snapshot(
            fingerprint,
            date(2026, 8, 14),
            "0.60",
            NOW + timedelta(days=3),
        ),
    ]
    store.replace_strategy_snapshots(fingerprint, records)
    assert store.current_strategy_snapshot(fingerprint, current_time).target_exposure == Decimal(  # type: ignore[union-attr]
        "1.60"
    )
    assert store.pending_strategy_snapshot(fingerprint, current_time).target_exposure == Decimal(  # type: ignore[union-attr]
        "0.60"
    )

    store.replace_strategy_snapshots(fingerprint, records[:1])
    assert len(store.strategy_snapshots(fingerprint)) == 1
    assert store.pending_strategy_snapshot(fingerprint, current_time) is None


def recommendation(status: str = "LIVE") -> RecommendationRecord:
    return RecommendationRecord(
        status=status,
        calculated_at=NOW,
        is_pending=False,
        config_fingerprint="a" * 64 if status == "LIVE" else None,
        es_quantity=1 if status == "LIVE" else None,
        mes_quantity=3 if status == "LIVE" else None,
        es_contract_con_id=1 if status == "LIVE" else None,
        es_contract_local_symbol="ESZ6" if status == "LIVE" else None,
        es_mark=Decimal("6500") if status == "LIVE" else None,
        es_mark_at=NOW if status == "LIVE" else None,
        es_multiplier=Decimal("50") if status == "LIVE" else None,
        mes_contract_con_id=2 if status == "LIVE" else None,
        mes_contract_local_symbol="MESZ6" if status == "LIVE" else None,
        mes_mark=Decimal("6500") if status == "LIVE" else None,
        mes_mark_at=NOW if status == "LIVE" else None,
        mes_multiplier=Decimal("5") if status == "LIVE" else None,
        strategy_amount_usd=Decimal("250000") if status == "LIVE" else None,
        target_exposure=Decimal("1.60") if status == "LIVE" else None,
        target_notional=Decimal("400000") if status == "LIVE" else None,
        achieved_notional=Decimal("390000") if status == "LIVE" else None,
        achieved_exposure=Decimal("1.56") if status == "LIVE" else None,
        target_shortfall=Decimal("0.04") if status == "LIVE" else None,
        target_effective_at=NOW if status == "LIVE" else None,
        reason=None if status == "LIVE" else "market data unavailable",
    )


def test_recommendations_and_audit_history_are_bounded(tmp_path: Path) -> None:
    store = repository(tmp_path)
    store.append_strategy_config(StrategyConfigRecord("a" * 64, "{}", NOW))
    live = store.append_recommendation(recommendation())
    assert live.id is not None
    store.append_recommendation(recommendation("UNAVAILABLE"))
    assert store.latest_recommendation().status == "UNAVAILABLE"  # type: ignore[union-attr]
    assert [item.status for item in store.recommendation_history(2)] == ["UNAVAILABLE", "LIVE"]

    store.append_audit(AuditEntry("SETTINGS_UPDATED", NOW, "{}"))
    assert store.audit_history(1)[0].action == "SETTINGS_UPDATED"
