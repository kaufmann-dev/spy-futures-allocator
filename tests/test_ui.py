from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.db import (
    Database,
    EventDocumentRecord,
    EventStateRecord,
    MesQuoteRecord,
    OperationalOverrides,
    RecommendationRecord,
    Repository,
    ScheduledTargetRecord,
    SignalSnapshotRecord,
    SpyBarRecord,
    build_dashboard_view,
)
from app.domain.schedule import XnysCalendar

NOW = datetime(2026, 8, 12, 22, 30, tzinfo=UTC)
CALENDAR = XnysCalendar()


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(Database(tmp_path / "app.sqlite3"))
    repository.initialize()
    repository.update_settings(strategy_amount_usd=Decimal("100000"), current_mes_quantity=5)
    return repository


def _quote(repository: Repository, *, at: datetime, market_type: int = 1) -> None:
    repository.append_mes_quote(
        MesQuoteRecord(
            con_id=7,
            bid=Decimal("6999.75"),
            ask=Decimal("7000.25"),
            last=Decimal("7000"),
            mark=Decimal("7000"),
            market_data_type=market_type,
            quoted_at=at,
            received_at=at,
        )
    )


def _recommendation(
    repository: Repository,
    *,
    status: str = "LIVE",
    pending: bool = False,
    target: Decimal = Decimal("3.5"),
    effective_at: datetime = NOW,
    quantity: int = 10,
) -> None:
    repository.append_recommendation(
        RecommendationRecord(
            status=status,
            calculated_at=NOW,
            is_pending=pending,
            quantity=quantity,
            contract_con_id=7,
            contract_local_symbol="MESU6",
            strategy_amount_usd=Decimal("100000"),
            mes_mark=Decimal("7000"),
            mes_mark_at=NOW,
            mes_multiplier=Decimal("5"),
            scheduled_target=target,
            event_cap=Decimal("4.85"),
            final_target=target,
            contract_notional=Decimal("35000"),
            target_notional=Decimal("350000"),
            achieved_notional=Decimal("350000"),
            achieved_leverage=Decimal("3.5"),
            target_shortfall=Decimal("0"),
            target_effective_at=effective_at,
        )
    )


def _pending_target(repository: Repository, *, target: Decimal, effective_at: datetime) -> None:
    repository.append_scheduled_target(
        ScheduledTargetRecord(
            signal_session_date=effective_at.date() - timedelta(days=1),
            target=target,
            effective_at=effective_at,
            state="PENDING",
            calculated_at=NOW,
        )
    )


def _sessions_ending_at(end_date: date, count: int) -> list[date]:
    sessions: list[date] = []
    candidate = end_date
    while len(sessions) < count:
        if CALENDAR.is_session(candidate):
            sessions.append(candidate)
        candidate -= timedelta(days=1)
    return list(reversed(sessions))


def _signal_history(
    repository: Repository,
    *,
    count: int = 260,
    start_close: Decimal = Decimal(400),
    daily_change: Decimal = Decimal("0.5"),
) -> None:
    for offset, session_date in enumerate(_sessions_ending_at(NOW.date(), count)):
        close = start_close + daily_change * offset
        previous_close = start_close + daily_change * max(0, offset - 1)
        raw_target = (
            Decimal(2)
            if session_date.weekday() == 0
            else Decimal(4)
            if session_date.weekday() == 3
            else Decimal("3.5")
        )
        _signal_session(
            repository,
            session_date=session_date,
            open_price=previous_close,
            close=close,
            raw_target=raw_target,
        )


def _signal_session(
    repository: Repository,
    *,
    session_date: date,
    open_price: Decimal,
    close: Decimal,
    raw_target: Decimal,
) -> None:
    sma200 = close - Decimal(20) if close > Decimal(20) else close
    repository.append_signal_snapshot(
        SignalSnapshotRecord(
            session_date=session_date,
            spy_close=close,
            sma200=sma200,
            distance_above_sma200=close / sma200 - Decimal(1),
            daily_return=Decimal("0.001"),
            rv20=Decimal("0.12"),
            prior_20_session_high=close + Decimal(2),
            dd20=Decimal("-0.005"),
            base=Decimal(4),
            bonus=Decimal("0.6"),
            volatility_cap=Decimal("8.3333"),
            raw_target=raw_target,
            shadow_raw_target=raw_target,
            calculated_at=CALENDAR.session_close(session_date) + timedelta(minutes=1),
        )
    )
    repository.append_spy_bar(
        SpyBarRecord(
            session_date=session_date,
            open=open_price,
            high=max(open_price, close),
            low=min(open_price, close),
            close=close,
            volume=Decimal("1000000"),
            completed_at=CALENDAR.session_close(session_date),
            fetched_at=CALENDAR.session_close(session_date) + timedelta(minutes=2),
        )
    )


def test_dashboard_has_exactly_eight_status_cells_and_live_result(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _quote(repository, at=NOW)
    _recommendation(repository)

    view = build_dashboard_view(repository, now=NOW + timedelta(seconds=20))

    assert len(view.statuses) == 8
    assert view.recommendation.headline == "HOLD NOW: 10 MES"
    assert view.recommendation.quantity_difference == 5
    assert view.recommendation.state == "LIVE"
    assert view.event.state == "NO_EVENT"
    assert isinstance(view.as_dict()["recommendation"]["quantity"], int)


def test_stale_quote_hides_current_quantity_and_labels_history(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _quote(repository, at=NOW - timedelta(minutes=2))
    _recommendation(repository)

    view = build_dashboard_view(repository, now=NOW)

    assert view.recommendation.state == "STALE"
    assert view.recommendation.headline == "RECOMMENDATION UNAVAILABLE"
    assert view.recommendation.quantity is None
    assert view.recommendation.historical_quantity == 10


def test_disconnected_ibkr_overrides_fresh_cached_quote(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _quote(repository, at=NOW)
    _recommendation(repository)

    view = build_dashboard_view(
        repository,
        now=NOW,
        operational=OperationalOverrides(ibkr_connection="UNAVAILABLE"),
    )

    assert view.recommendation.state == "STALE"
    assert view.recommendation.headline == "RECOMMENDATION UNAVAILABLE"
    assert view.recommendation.quantity is None
    assert view.recommendation.historical_quantity == 10
    assert view.statuses[0].state == "UNAVAILABLE"


def test_delayed_market_data_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _quote(repository, at=NOW, market_type=3)
    _recommendation(repository)

    view = build_dashboard_view(repository, now=NOW)

    assert view.recommendation.state == "UNAVAILABLE"
    assert view.statuses[2].state == "NOT_ENTITLED"
    assert view.statuses[3].value == "Delayed"


def test_matching_future_pending_recommendation_is_displayed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    effective_at = NOW + timedelta(hours=1)
    _quote(repository, at=NOW)
    _recommendation(repository)
    _pending_target(repository, target=Decimal("2.5"), effective_at=effective_at)
    _recommendation(
        repository,
        pending=True,
        target=Decimal("2.5"),
        effective_at=effective_at,
        quantity=7,
    )

    view = build_dashboard_view(repository, now=NOW)

    assert view.pending is not None
    assert view.pending.quantity == 7
    assert view.pending.target == "2.50×"


def test_expired_pending_recommendation_is_not_displayed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    effective_at = NOW + timedelta(hours=1)
    _recommendation(repository)
    _pending_target(repository, target=Decimal("2.5"), effective_at=effective_at)
    _recommendation(
        repository,
        pending=True,
        target=Decimal("2.5"),
        effective_at=effective_at,
        quantity=7,
    )
    after_effective_at = effective_at + timedelta(seconds=1)
    _quote(repository, at=after_effective_at)

    view = build_dashboard_view(repository, now=after_effective_at)

    assert view.pending is None


def test_pending_recommendation_must_match_current_future_target(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    effective_at = NOW + timedelta(hours=1)
    _quote(repository, at=NOW)
    _recommendation(repository)
    _pending_target(repository, target=Decimal("2.5"), effective_at=effective_at)
    _recommendation(
        repository,
        pending=True,
        target=Decimal("3.0"),
        effective_at=effective_at,
        quantity=8,
    )

    view = build_dashboard_view(repository, now=NOW)

    assert view.pending is None


def test_strategy_chart_aligns_price_risk_and_leverage_series(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _signal_history(repository)

    view = build_dashboard_view(repository, now=NOW)

    chart = view.strategy_chart
    expected_sessions = _sessions_ending_at(NOW.date(), 260)[-252:]
    assert chart.available
    assert chart.session_count == 252
    assert chart.start_date == expected_sessions[0].isoformat()
    assert chart.end_date == expected_sessions[-1].isoformat()
    assert chart.latest_close == "529.50"
    assert chart.latest_sma200 == "509.50"
    assert chart.latest_rv20 == "12.00%"
    assert chart.latest_dd20 == "-0.50%"
    assert chart.latest_raw_target == "3.50×"
    assert chart.latest_effective_target == "2.00×"
    assert chart.price_relationship == "SPY is 3.93% above its SMA200."
    assert len(chart.price_close_points.split()) == 252
    assert len(chart.price_sma200_points.split()) == 252
    assert len(chart.risk_rv20_points.split()) == 252
    assert len(chart.risk_dd20_points.split()) == 252
    assert len(chart.leverage_raw_target_points.split()) == 252
    assert chart.leverage_effective_target_path.startswith("M ")
    assert " H " in chart.leverage_effective_target_path
    assert " V " in chart.leverage_effective_target_path
    assert " L " not in chart.leverage_effective_target_path
    assert chart.performance_available
    assert len(chart.performance_strategy_points.split()) == 252
    assert len(chart.performance_spy_points.split()) == 252
    assert len(chart.drawdown_strategy_points.split()) == 252
    assert len(chart.drawdown_spy_points.split()) == 252
    assert chart.exposure_open_path.startswith("M ")
    assert chart.latest_strategy_value != "—"
    assert chart.latest_spy_value != "—"
    assert view.as_dict()["strategy_chart"]["session_count"] == 252


def test_strategy_chart_waits_for_two_calculations(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    view = build_dashboard_view(repository, now=NOW)

    assert not view.strategy_chart.available
    assert view.strategy_chart.empty_message == (
        "Waiting for two completed strategy calculations (0 stored)."
    )


def test_strategy_chart_handles_flat_series_without_invalid_coordinates(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _signal_history(
        repository,
        count=3,
        start_close=Decimal(420),
        daily_change=Decimal(0),
    )

    view = build_dashboard_view(repository, now=NOW)

    chart = view.strategy_chart
    assert chart.available
    assert chart.session_count == 2
    assert chart.latest_close == "420.00"
    serialized = str(chart.as_dict())
    assert "NaN" not in serialized
    assert "Infinity" not in serialized


def test_strategy_chart_applies_event_lock_on_its_evaluated_session(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _signal_history(repository)
    lock_session = _sessions_ending_at(NOW.date(), 2)[0]
    repository.append_event_state(
        EventStateRecord(
            state="WINDOW_ACTIVE",
            event_cap=Decimal("4.85"),
            occurred_at=NOW,
        )
    )
    repository.append_event_state(
        EventStateRecord(
            state="EVENT_LOCKED",
            event_cap=Decimal(0),
            occurred_at=NOW,
            event_drawdown=Decimal("-0.04"),
            lock_session=lock_session,
            last_evaluated_session=lock_session,
        )
    )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert chart.event_lock_reference_visible
    assert chart.latest_effective_target == "0.00×"
    assert chart.event_lock_y


def test_strategy_chart_restores_leverage_only_at_persisted_recovery_release(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _signal_history(repository)
    lock_session, release_session = _sessions_ending_at(NOW.date(), 4)[::3]
    release_at = CALENDAR.session_open(release_session)
    repository.append_event_state(
        EventStateRecord(
            state="EVENT_LOCKED",
            event_cap=Decimal(0),
            occurred_at=NOW - timedelta(minutes=1),
            lock_session=lock_session,
            last_evaluated_session=lock_session,
        )
    )
    repository.append_event_state(
        EventStateRecord(
            state="NO_EVENT",
            event_cap=Decimal("4.85"),
            occurred_at=NOW,
            lock_session=lock_session,
            last_evaluated_session=lock_session,
            release_at=release_at,
        )
    )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert chart.latest_effective_target != "0.00×"
    assert chart.event_lock_reference_visible


def test_exposure_chart_fails_closed_when_a_strategy_session_is_missing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _signal_history(repository)
    missing_session = _sessions_ending_at(NOW.date(), 120)[40]
    with repository.database.transaction() as connection:
        connection.execute(
            "DELETE FROM signal_snapshots WHERE session_date = ?",
            (missing_session.isoformat(),),
        )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert chart.available
    assert not chart.performance_available
    assert chart.exposure_open_path == ""
    assert chart.exposure_invested_percent == "—"
    assert chart.latest_open_exposure == "—"
    assert chart.exposure_empty_message == (
        "Historical exposure unavailable: completed strategy sessions contain a gap."
    )


def test_strategy_chart_fails_closed_for_a_lock_without_a_market_session(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _signal_history(repository)
    repository.append_event_state(
        EventStateRecord(
            state="EVENT_LOCKED",
            event_cap=Decimal(0),
            occurred_at=NOW,
        )
    )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert not chart.available
    assert chart.empty_message == (
        "Historical strategy replay unavailable: EVENT_LOCKED has no valid evaluated "
        "market session."
    )


def test_rule_replay_includes_lock_close_and_splits_recovery_open_timing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    sessions = [
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 7),
        date(2026, 8, 10),
        date(2026, 8, 11),
    ]
    opens = [
        Decimal(100),
        Decimal(100),
        Decimal(100),
        Decimal(100),
        Decimal(81),
        Decimal(81),
        Decimal(81),
    ]
    closes = [
        Decimal(100),
        Decimal(100),
        Decimal(100),
        Decimal(90),
        Decimal(81),
        Decimal(81),
        Decimal("89.1"),
    ]
    raw_targets = [
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(2),
        Decimal(2),
    ]
    for session_date, open_price, close, raw_target in zip(
        sessions, opens, closes, raw_targets, strict=True
    ):
        _signal_session(
            repository,
            session_date=session_date,
            open_price=open_price,
            close=close,
            raw_target=raw_target,
        )
    repository.append_event_state(
        EventStateRecord(
            state="EVENT_LOCKED",
            event_cap=Decimal(0),
            occurred_at=NOW - timedelta(minutes=1),
            lock_session=date(2026, 8, 6),
            last_evaluated_session=date(2026, 8, 6),
        )
    )
    repository.append_event_state(
        EventStateRecord(
            state="NO_EVENT",
            event_cap=Decimal("4.85"),
            occurred_at=NOW,
            lock_session=date(2026, 8, 6),
            last_evaluated_session=date(2026, 8, 6),
            release_at=CALENDAR.session_open(date(2026, 8, 11)),
        )
    )

    chart = build_dashboard_view(repository, now=NOW).strategy_chart

    assert chart.performance_available
    assert chart.latest_strategy_value == "$108.00"
    assert chart.latest_spy_value == "$89.10"
    assert chart.exposure_zero_sessions == 2
    assert chart.latest_open_exposure == "2.00×"
    assert chart.exposure_open_path == (
        "M 0.00,79.38 H 200.00 V 79.38 H 400.00 V 79.38 "
        "H 600.00 V 100.00 H 800.00 V 100.00 H 1000.00 V 58.76"
    )


def test_dashboard_template_escapes_data_and_has_required_disclosures(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _quote(repository, at=NOW)
    _recommendation(repository)
    _signal_history(repository)
    repository.append_event_document(
        EventDocumentRecord(
            event_case_id=None,
            model_run_id=None,
            url="javascript:alert(1)",
            title="<script>alert(1)</script>",
            publisher="Unsafe",
            source_class="OTHER",
            published_at=None,
            retrieved_at=NOW,
            content_hash="abc",
        )
    )
    view = build_dashboard_view(repository, now=NOW)
    templates = Path(__file__).parents[1] / "app" / "templates"
    environment = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(["html"]),
    )
    html = environment.get_template("dashboard.html").render(view=view, chart_view="performance")

    assert "HOLD NOW: 10 MES" in html
    assert 'href="/static/app.css"' in html
    assert "http://" not in html
    assert "NOTIONAL SIZING ONLY — MARGIN NOT CHECKED" in html
    assert "It cannot place, modify, approve, or cancel orders." in html
    assert html.count('class="status-cell"') == 8
    assert "Strategy history" in html
    assert "252 sessions" in html
    assert html.count('class="chart-band"') == 1
    assert html.count("<svg") == 1
    assert html.count("<polyline") == 3
    assert html.count('role="img"') == 1
    assert 'aria-labelledby="strategy-performance-title strategy-performance-description"' in html
    assert "ARL-5" in html
    assert "SPY" in html
    assert "Fractional exposure; not MES performance" in html
    assert html.count('name="chart_view"') == 6
    assert html.count('aria-current="true"') == 1
    assert html.count(" selected") == 1
    assert 'class="chart-view-select"' in html
    assert 'class="chart-view-buttons"' in html
    assert "<script" not in html
    assert "onchange=" not in html
    assert 'class="chart-line chart-line-effective"' in html
    assert 'class="chart-line chart-line-secondary-halo"' in html


def test_dashboard_renders_exactly_one_accessible_chart_for_each_view(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _signal_history(repository)
    view = build_dashboard_view(repository, now=NOW)
    templates = Path(__file__).parents[1] / "app" / "templates"
    environment = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(["html"]),
    )
    expected_titles = {
        "performance": "Historical ARL-5 rule replay compared with SPY",
        "drawdowns": "Historical ARL-5 and SPY drawdowns",
        "exposure": "Historical regular-session target exposure",
        "trend": "SPY close and 200-session moving average",
        "risk": "RV20 volatility and DD20 drawdown",
    }

    for chart_view, title in expected_titles.items():
        html = environment.get_template("dashboard.html").render(view=view, chart_view=chart_view)

        assert html.count("<svg") == 1
        assert html.count('role="img"') == 1
        assert title in html
        assert html.count('aria-current="true"') == 1
