from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.db import (
    Database,
    EventDocumentRecord,
    MesQuoteRecord,
    OperationalOverrides,
    RecommendationRecord,
    Repository,
    ScheduledTargetRecord,
    SpyBarRecord,
    build_dashboard_view,
)

NOW = datetime(2026, 8, 12, 14, 30, tzinfo=UTC)


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


def _spy_history(
    repository: Repository,
    *,
    count: int = 452,
    start_close: Decimal = Decimal(100),
    daily_change: Decimal = Decimal(1),
) -> None:
    start = NOW - timedelta(days=count - 1)
    for offset in range(count):
        observed_at = start + timedelta(days=offset)
        close = start_close + daily_change * offset
        repository.append_spy_bar(
            SpyBarRecord(
                session_date=observed_at.date(),
                open=close - Decimal("1"),
                high=close + Decimal("1"),
                low=close - Decimal("2"),
                close=close,
                volume=Decimal("1000000"),
                completed_at=observed_at,
                fetched_at=observed_at,
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


def test_spy_trend_uses_one_year_and_exact_rolling_sma(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _spy_history(repository)

    view = build_dashboard_view(repository, now=NOW)

    assert view.spy_trend.available
    assert view.spy_trend.session_count == 252
    assert view.spy_trend.start_date == (NOW - timedelta(days=251)).date().isoformat()
    assert view.spy_trend.end_date == NOW.date().isoformat()
    assert view.spy_trend.latest_close == "551.00"
    assert view.spy_trend.latest_sma200 == "451.50"
    assert view.spy_trend.relationship == "SPY is 22.04% above its SMA200."
    assert len(view.spy_trend.close_points.split()) == 252
    assert len(view.spy_trend.sma_points.split()) == 252
    assert view.as_dict()["spy_trend"]["session_count"] == 252


def test_spy_trend_waits_for_sufficient_completed_history(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _spy_history(repository, count=199)

    view = build_dashboard_view(repository, now=NOW)

    assert not view.spy_trend.available
    assert view.spy_trend.empty_message == ("Waiting for 200 completed SPY sessions (199 stored).")


def test_spy_trend_waits_for_a_drawable_second_point(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _spy_history(repository, count=200)

    view = build_dashboard_view(repository, now=NOW)

    assert not view.spy_trend.available
    assert view.spy_trend.session_count == 1
    assert view.spy_trend.latest_close == "299.00"
    assert view.spy_trend.latest_sma200 == "199.50"
    assert view.spy_trend.empty_message == (
        "One SMA200 point is available; one more completed session is needed to draw the trend."
    )


def test_spy_trend_handles_flat_prices_without_invalid_coordinates(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _spy_history(
        repository,
        count=201,
        start_close=Decimal(420),
        daily_change=Decimal(0),
    )

    view = build_dashboard_view(repository, now=NOW)

    assert view.spy_trend.available
    assert view.spy_trend.session_count == 2
    assert view.spy_trend.latest_close == "420.00"
    assert view.spy_trend.latest_sma200 == "420.00"
    assert view.spy_trend.relationship == "SPY is exactly at its SMA200."
    assert "NaN" not in view.spy_trend.close_points
    assert "Infinity" not in view.spy_trend.sma_points


def test_dashboard_template_escapes_data_and_has_required_disclosures(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _quote(repository, at=NOW)
    _recommendation(repository)
    _spy_history(repository)
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
    html = environment.get_template("dashboard.html").render(view=view)

    assert "HOLD NOW: 10 MES" in html
    assert 'href="/static/app.css"' in html
    assert "http://" not in html
    assert "NOTIONAL SIZING ONLY — MARGIN NOT CHECKED" in html
    assert "It cannot place, modify, approve, or cancel orders." in html
    assert html.count('class="status-cell"') == 8
    assert "SPY close and SMA200" in html
    assert "252 completed sessions" in html
    assert html.count("<svg") == 1
    assert html.count("<polyline") == 2
    assert 'role="img"' in html
    assert "SPY close · solid" in html
    assert "SMA200 · dashed" in html
    assert "SPY is 22.04% above its SMA200." in html
