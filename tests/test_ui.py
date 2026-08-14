from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.db import (
    ContractRecord,
    Database,
    FuturesQuoteRecord,
    OperationalOverrides,
    RecommendationRecord,
    Repository,
    SpyBarRecord,
    StrategyConfigRecord,
    StrategySnapshotRecord,
    build_dashboard_view,
)
from app.services.strategy_config import (
    serialize_strategy_config,
    strategy_config_fingerprint,
    strategy_config_from_settings,
)

NOW = datetime(2026, 8, 14, 20, tzinfo=UTC)
ACTIVE_EXPIRY = "20260918"
ES_CON_ID = 11
MES_CON_ID = 12
CSRF_VALUE = "test-csrf-value"  # noqa: S105


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(Database(tmp_path / "allocator.sqlite3"))
    repository.initialize()
    return repository


def _configured_repository(tmp_path: Path) -> tuple[Repository, str]:
    repository = _repository(tmp_path)
    settings = repository.update_settings(
        strategy_amount_usd=Decimal("100000"),
        current_es_quantity=0,
        current_mes_quantity=1,
    )
    config = strategy_config_from_settings(settings)
    fingerprint = strategy_config_fingerprint(config)
    repository.append_strategy_config(
        StrategyConfigRecord(
            fingerprint=fingerprint,
            config_json=serialize_strategy_config(config),
            created_at=NOW,
        )
    )
    return repository, fingerprint


def _append_contracts(repository: Repository, *, expiry: str = ACTIVE_EXPIRY) -> None:
    repository.append_contract(
        ContractRecord(
            con_id=ES_CON_ID,
            symbol="ES",
            local_symbol="ESU6",
            trading_class="ES",
            exchange="CME",
            currency="USD",
            last_trade_date=expiry,
            multiplier=Decimal(50),
            resolved_at=NOW,
        )
    )
    repository.append_contract(
        ContractRecord(
            con_id=MES_CON_ID,
            symbol="MES",
            local_symbol="MESU6",
            trading_class="MES",
            exchange="CME",
            currency="USD",
            last_trade_date=expiry,
            multiplier=Decimal(5),
            resolved_at=NOW,
        )
    )


def _append_quote(
    repository: Repository,
    symbol: str,
    *,
    at: datetime = NOW,
    con_id: int | None = None,
    market_data_type: int = 1,
    mark: Decimal = Decimal(5000),
) -> None:
    expected_id = ES_CON_ID if symbol == "ES" else MES_CON_ID
    repository.append_futures_quote(
        FuturesQuoteRecord(
            con_id=expected_id if con_id is None else con_id,
            symbol=symbol,
            bid=mark - Decimal("0.25"),
            ask=mark + Decimal("0.25"),
            last=mark,
            mark=mark,
            market_data_type=market_data_type,
            quoted_at=at,
            received_at=at,
        )
    )


def _append_market(
    repository: Repository,
    *,
    expiry: str = ACTIVE_EXPIRY,
    es_at: datetime = NOW,
    mes_at: datetime = NOW,
    es_con_id: int | None = None,
    mes_con_id: int | None = None,
    es_market_data_type: int = 1,
    mes_market_data_type: int = 1,
    include_es: bool = True,
    include_mes: bool = True,
) -> None:
    _append_contracts(repository, expiry=expiry)
    if include_es:
        _append_quote(
            repository,
            "ES",
            at=es_at,
            con_id=es_con_id,
            market_data_type=es_market_data_type,
        )
    if include_mes:
        _append_quote(
            repository,
            "MES",
            at=mes_at,
            con_id=mes_con_id,
            market_data_type=mes_market_data_type,
        )


def _recommendation(
    fingerprint: str,
    *,
    status: str = "LIVE",
    pending: bool = False,
    target: Decimal = Decimal("1.60"),
    effective_at: datetime = NOW - timedelta(hours=1),
    mark_at: datetime = NOW,
    es_con_id: int = ES_CON_ID,
    mes_con_id: int = MES_CON_ID,
    calculated_at: datetime = NOW,
) -> RecommendationRecord:
    es_quantity = 0
    mes_quantity = 6 if target == Decimal("1.60") else 2
    achieved_notional = Decimal(mes_quantity) * Decimal(5000) * Decimal(5)
    return RecommendationRecord(
        status=status,
        calculated_at=calculated_at,
        is_pending=pending,
        config_fingerprint=fingerprint,
        es_quantity=es_quantity,
        mes_quantity=mes_quantity,
        es_contract_con_id=es_con_id,
        es_contract_local_symbol="ESU6",
        es_mark=Decimal(5000),
        es_mark_at=mark_at,
        es_multiplier=Decimal(50),
        mes_contract_con_id=mes_con_id,
        mes_contract_local_symbol="MESU6",
        mes_mark=Decimal(5000),
        mes_mark_at=mark_at,
        mes_multiplier=Decimal(5),
        strategy_amount_usd=Decimal(100000),
        target_exposure=target,
        target_notional=Decimal(100000) * target,
        achieved_notional=achieved_notional,
        achieved_exposure=achieved_notional / Decimal(100000),
        target_shortfall=Decimal(100000) * target - achieved_notional,
        target_effective_at=effective_at,
        reason="Risk-on with normal realized volatility.",
    )


def _unavailable_recommendation() -> RecommendationRecord:
    return RecommendationRecord(
        status="UNAVAILABLE",
        calculated_at=NOW,
        is_pending=False,
        config_fingerprint=None,
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
        strategy_amount_usd=None,
        target_exposure=None,
        target_notional=None,
        achieved_notional=None,
        achieved_exposure=None,
        target_shortfall=None,
        target_effective_at=None,
        reason="market data unavailable",
    )


def _operational(fingerprint: str, *, connection: str = "LIVE") -> OperationalOverrides:
    return OperationalOverrides(
        ibkr_connection=connection,
        config_fingerprint=fingerprint,
        next_regime_evaluation=NOW + timedelta(hours=1),
        next_volatility_review=NOW + timedelta(days=3),
        next_market_open=NOW + timedelta(days=3, hours=13),
    )


def _sessions(count: int) -> list[date]:
    sessions: list[date] = []
    candidate = date(2025, 1, 2)
    while len(sessions) < count:
        if candidate.weekday() < 5:
            sessions.append(candidate)
        candidate += timedelta(days=1)
    return sessions


def _append_strategy_history(
    repository: Repository,
    fingerprint: str,
    closes: list[Decimal],
    targets: list[Decimal],
) -> tuple[list[date], list[StrategySnapshotRecord]]:
    assert len(closes) == len(targets)
    sessions = _sessions(len(closes))
    snapshots: list[StrategySnapshotRecord] = []
    previous_close = closes[0]
    for index, (session, close, target) in enumerate(
        zip(sessions, closes, targets, strict=True)
    ):
        completed_at = datetime(
            session.year,
            session.month,
            session.day,
            21,
            tzinfo=UTC,
        )
        repository.upsert_spy_bar(
            SpyBarRecord(
                session_date=session,
                open=previous_close,
                high=max(previous_close, close) + Decimal(1),
                low=min(previous_close, close) - Decimal(1),
                close=close,
                volume=Decimal(1_000_000),
                completed_at=completed_at,
                fetched_at=completed_at + timedelta(minutes=1),
            )
        )
        snapshots.append(
            StrategySnapshotRecord(
                config_fingerprint=fingerprint,
                session_date=session,
                spy_close=close,
                fast_sma=close - Decimal(1),
                slow_sma=close - Decimal(2),
                rv=Decimal("0.18") + Decimal(index % 3) / Decimal(100),
                regime_state="RISK_OFF" if target == 0 else "RISK_ON",
                exit_armed=target != 0,
                post_exit_low=close if target == 0 else None,
                reentry_level=close * Decimal("1.25") if target == 0 else None,
                volatility_state="HIGH" if target == Decimal("0.60") else "NORMAL",
                target_exposure=target,
                effective_at=completed_at + timedelta(days=1),
                calculated_at=completed_at + timedelta(minutes=2),
            )
        )
        previous_close = close
    repository.replace_strategy_snapshots(fingerprint, snapshots)
    return sessions, snapshots


def test_live_dashboard_exposes_dual_allocation_and_exact_settings(tmp_path: Path) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    _append_market(repository)
    repository.append_recommendation(_recommendation(fingerprint))

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint),
        csrf_token=CSRF_VALUE,
    )

    assert view.recommendation.state == "LIVE"
    assert view.recommendation.headline == "HOLD NOW: 0 ES + 6 MES"
    assert view.recommendation.historical_allocation is None
    assert view.recommendation.es_difference == "0"
    assert view.recommendation.mes_difference == "+5"
    assert view.recommendation.es_contract == "ESU6"
    assert view.recommendation.mes_contract == "MESU6"
    assert {item.label for item in view.statuses} >= {
        "ES quote freshness",
        "MES quote freshness",
        "ES contract",
        "MES contract",
        "Next regime evaluation",
        "Next volatility review",
        "Next market open",
    }
    assert view.settings.fast_sma_sessions == 100
    assert view.settings.slow_sma_sessions == 500
    assert view.settings.rebound_percent == "25.00"
    assert view.settings.rv_threshold_percent == "25.00"
    assert view.settings.normal_exposure == "1.60"
    assert view.settings.high_vol_exposure == "0.60"
    assert view.settings.review_weekdays == (0, 3)
    assert view.csrf_token == CSRF_VALUE
    assert view.as_dict()["recommendation"]["headline"] == "HOLD NOW: 0 ES + 6 MES"


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        ("disconnected", "STALE"),
        ("delayed", "UNAVAILABLE"),
        ("missing_quote", "UNAVAILABLE"),
        ("stale_quote", "STALE"),
        ("future_quote", "UNAVAILABLE"),
        ("wrong_quote_contract", "UNAVAILABLE"),
        ("wrong_saved_contract", "UNAVAILABLE"),
        ("wrong_fingerprint", "UNAVAILABLE"),
        ("saved_stale", "STALE"),
        ("inactive_expiry", "UNAVAILABLE"),
    ],
)
def test_recommendation_fails_closed_and_preserves_historical_allocation(
    tmp_path: Path,
    failure: str,
    expected_state: str,
) -> None:
    repository, fingerprint = _configured_repository(tmp_path / failure)
    quote_at = NOW
    expiry = ACTIVE_EXPIRY
    es_type = 1
    include_mes = True
    es_quote_con_id: int | None = None
    if failure == "delayed":
        es_type = 3
    elif failure == "missing_quote":
        include_mes = False
    elif failure == "stale_quote":
        quote_at = NOW - timedelta(seconds=61)
    elif failure == "future_quote":
        quote_at = NOW + timedelta(seconds=6)
    elif failure == "wrong_quote_contract":
        es_quote_con_id = 999
    elif failure == "inactive_expiry":
        expiry = "20260619"
    _append_market(
        repository,
        expiry=expiry,
        es_at=quote_at,
        mes_at=quote_at,
        es_con_id=es_quote_con_id,
        es_market_data_type=es_type,
        include_mes=include_mes,
    )

    record_fingerprint = fingerprint
    if failure == "wrong_fingerprint":
        record_fingerprint = "b" * 64
        repository.append_strategy_config(
            StrategyConfigRecord(record_fingerprint, "{}", NOW)
        )
    status = "STALE" if failure == "saved_stale" else "LIVE"
    es_saved_con_id = 999 if failure == "wrong_saved_contract" else ES_CON_ID
    repository.append_recommendation(
        _recommendation(
            record_fingerprint,
            status=status,
            mark_at=quote_at,
            es_con_id=es_saved_con_id,
        )
    )
    connection = "UNAVAILABLE" if failure == "disconnected" else "LIVE"

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint, connection=connection),
    )

    assert view.recommendation.state == expected_state
    assert view.recommendation.headline == "RECOMMENDATION UNAVAILABLE"
    assert view.recommendation.historical_allocation == "0 ES + 6 MES"
    assert view.recommendation.es_difference == "—"
    assert view.recommendation.mes_difference == "—"


def test_fresh_unrelated_quotes_do_not_revive_an_old_sizing_record(tmp_path: Path) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    _append_contracts(repository)
    old_time = NOW - timedelta(minutes=5)
    repository.append_recommendation(
        _recommendation(fingerprint, mark_at=old_time, calculated_at=old_time)
    )
    _append_quote(repository, "ES", at=NOW)
    _append_quote(repository, "MES", at=NOW)

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint),
    )

    assert view.recommendation.state == "UNAVAILABLE"
    assert view.recommendation.historical_allocation == "0 ES + 6 MES"
    assert view.recommendation.reason == (
        "The saved allocation does not use the latest validated quotes."
    )


def test_incomplete_latest_record_uses_last_complete_allocation_for_history(
    tmp_path: Path,
) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    _append_market(repository)
    historical_time = NOW - timedelta(minutes=2)
    repository.append_recommendation(
        _recommendation(
            fingerprint,
            mark_at=historical_time,
            calculated_at=historical_time,
        )
    )
    for _ in range(12):
        repository.append_recommendation(_unavailable_recommendation())

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint),
    )

    assert view.recommendation.state == "UNAVAILABLE"
    assert view.recommendation.reason == "market data unavailable"
    assert view.recommendation.historical_allocation == "0 ES + 6 MES"
    assert view.recommendation.es_contract == "ESU6"
    assert view.recommendation.es_mark == "5,000.00"
    assert view.recommendation.target_exposure == "1.60×"


def test_pending_allocation_requires_live_distinct_future_current_config(
    tmp_path: Path,
) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    _append_market(repository)
    repository.append_recommendation(_recommendation(fingerprint))
    repository.append_recommendation(
        _recommendation(
            fingerprint,
            pending=True,
            target=Decimal("0.60"),
            effective_at=NOW + timedelta(hours=1),
        )
    )

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint),
    )

    assert view.pending is not None
    assert view.pending.headline == "HOLD NEXT: 0 ES + 2 MES"
    assert view.pending.target_exposure == "0.60×"


@pytest.mark.parametrize("failure", ["status", "fingerprint", "expired", "same_target"])
def test_invalid_pending_allocation_is_hidden(tmp_path: Path, failure: str) -> None:
    repository, fingerprint = _configured_repository(tmp_path / failure)
    _append_market(repository)
    repository.append_recommendation(_recommendation(fingerprint))
    pending_fingerprint = fingerprint
    if failure == "fingerprint":
        pending_fingerprint = "c" * 64
        repository.append_strategy_config(
            StrategyConfigRecord(pending_fingerprint, "{}", NOW)
        )
    repository.append_recommendation(
        _recommendation(
            pending_fingerprint,
            pending=True,
            status="STALE" if failure == "status" else "LIVE",
            target=Decimal("1.60") if failure == "same_target" else Decimal("0.60"),
            effective_at=(
                NOW - timedelta(seconds=1)
                if failure == "expired"
                else NOW + timedelta(hours=1)
            ),
        )
    )

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint),
    )

    assert view.pending is None


def test_chart_applies_each_close_target_at_the_next_observed_open(tmp_path: Path) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    _append_strategy_history(
        repository,
        fingerprint,
        [Decimal(100), Decimal(110), Decimal(99)],
        [Decimal(2), Decimal(0), Decimal(1)],
    )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert chart.available
    assert chart.session_count == 3
    assert chart.latest_strategy_value == "$120.00"
    assert chart.latest_spy_value == "$99.00"
    assert chart.strategy_total_return == "20.00%"
    assert chart.spy_total_return == "-1.00%"
    assert chart.latest_exposure == "0.00×"
    assert chart.risk_off_sessions == 1
    assert chart.risk_on_percent == "66.67%"
    assert len(chart.performance_strategy_points.split()) == 3
    assert len(chart.drawdown_strategy_points.split()) == 3
    assert chart.exposure_path.startswith("M ")
    assert " H " in chart.exposure_path
    assert " V " in chart.exposure_path


def test_chart_uses_latest_252_sessions_for_only_the_current_fingerprint(
    tmp_path: Path,
) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    closes = [Decimal(400) + Decimal(index) / Decimal(10) for index in range(260)]
    targets = [Decimal(1) if index % 2 == 0 else Decimal("0.60") for index in range(260)]
    sessions, snapshots = _append_strategy_history(
        repository,
        fingerprint,
        closes,
        targets,
    )
    other_fingerprint = "d" * 64
    repository.append_strategy_config(StrategyConfigRecord(other_fingerprint, "{}", NOW))
    repository.replace_strategy_snapshots(
        other_fingerprint,
        [
            replace(
                snapshot,
                config_fingerprint=other_fingerprint,
                target_exposure=Decimal(9),
            )
            for snapshot in snapshots
        ],
    )

    chart = build_dashboard_view(
        repository,
        now=NOW,
        operational=OperationalOverrides(config_fingerprint=fingerprint),
    ).strategy_chart

    assert chart.available
    assert chart.session_count == 252
    assert chart.start_date == sessions[-252].isoformat()
    assert chart.end_date == sessions[-1].isoformat()
    assert chart.latest_exposure == "1.00×"
    assert len(chart.performance_strategy_points.split()) == 252
    assert len(chart.performance_spy_points.split()) == 252
    assert len(chart.price_close_points.split()) == 252
    assert len(chart.price_fast_sma_points.split()) == 252
    assert len(chart.price_slow_sma_points.split()) == 252
    assert len(chart.risk_rv_points.split()) == 252
    assert chart.fast_sma_label == "SMA100"
    assert chart.slow_sma_label == "SMA500"
    assert chart.rv_label == "RV20"
    assert chart.rv_threshold_label == "25.00%"


def test_chart_fails_closed_when_current_snapshot_lacks_aligned_ohlc(
    tmp_path: Path,
) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    sessions, _ = _append_strategy_history(
        repository,
        fingerprint,
        [Decimal(100), Decimal(101), Decimal(102)],
        [Decimal(1), Decimal(1), Decimal(1)],
    )
    with repository.database.transaction() as connection:
        connection.execute(
            "DELETE FROM spy_bars WHERE session_date = ?",
            (sessions[1].isoformat(),),
        )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert not chart.available
    assert chart.performance_available is False
    assert chart.empty_message == (
        f"Historical strategy replay unavailable: completed SPY OHLC is missing for "
        f"{sessions[1].isoformat()}."
    )


def test_dashboard_template_renders_one_accessible_scriptless_chart_per_view(
    tmp_path: Path,
) -> None:
    repository, fingerprint = _configured_repository(tmp_path)
    _append_market(repository)
    repository.append_recommendation(_recommendation(fingerprint))
    _append_strategy_history(
        repository,
        fingerprint,
        [Decimal(100), Decimal(101), Decimal(102)],
        [Decimal("1.60"), Decimal("0.60"), Decimal("1.60")],
    )
    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=_operational(fingerprint),
        csrf_token=CSRF_VALUE,
    )
    templates = Path(__file__).parents[1] / "app" / "templates"
    environment = Environment(
        loader=FileSystemLoader(templates),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html"]),
    )
    expected_titles = {
        "performance": "Historical strategy performance compared with SPY",
        "drawdowns": "Historical strategy and SPY drawdowns",
        "exposure": "Historical target exposure",
        "trend": "SPY close with fast and slow moving averages",
        "risk": "SPY realized volatility",
    }

    for chart_view, title in expected_titles.items():
        html = environment.get_template("dashboard.html").render(
            view=view,
            chart_view=chart_view,
            message=None,
            error=None,
        )

        assert html.count("<svg") == 1
        assert html.count('role="img"') == 1
        assert title in html
        assert html.count('aria-current="true"') == 1
        assert html.count('name="chart_view"') == 6
        assert 'action="/settings/reset-strategy"' in html
        assert 'name="current_es_quantity"' in html
        assert 'name="review_weekdays"' in html
        assert "/static/spy-futures-allocator-mark.svg" in html
        assert "ARL-5" not in html
        assert "OpenAI" not in html
        assert "Systemic event" not in html
        assert "<script" not in html
