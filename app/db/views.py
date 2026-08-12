"""Read-only, JSON-safe view models for the API and server-rendered page."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.domain.calculation import MAX_TARGET
from app.domain.performance import (
    ExposureSchedulePoint,
    PerformanceSeries,
    PerformanceUnavailable,
    SpyOhlcBar,
    calculate_performance,
)
from app.domain.schedule import XnysCalendar, signal_window_for

from .models import EventStateRecord, RecommendationRecord, SignalSnapshotRecord, SpyBarRecord
from .repository import Repository

VIENNA = ZoneInfo("Europe/Vienna")
XNYS = XnysCalendar()


def _display_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(VIENNA).strftime("%Y-%m-%d %H:%M:%S %Z")


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _price(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _multiple(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}×"


def _percentage(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _safe_url(url: str) -> str | None:
    parsed = urlsplit(url)
    return url if parsed.scheme == "https" and bool(parsed.netloc) else None


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _iso_api(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _iso_api(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SerializableView:
    def as_dict(self) -> dict[str, Any]:
        value = _json_value(asdict(cast(Any, self)))
        if not isinstance(value, dict):  # pragma: no cover - asdict always returns a mapping
            raise TypeError("view did not serialize to an object")
        return cast(dict[str, Any], value)


@dataclass(frozen=True, slots=True)
class StatusCell(SerializableView):
    label: str
    value: str
    state: str


@dataclass(frozen=True, slots=True)
class MetricRow(SerializableView):
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class StrategyChartView(SerializableView):
    available: bool
    performance_available: bool
    performance_empty_message: str | None
    session_count: int
    start_date: str
    end_date: str
    performance_strategy_points: str
    performance_spy_points: str
    performance_high_label: str
    performance_midpoint_label: str
    performance_low_label: str
    latest_strategy_value: str
    latest_spy_value: str
    strategy_total_return: str
    spy_total_return: str
    drawdown_strategy_points: str
    drawdown_spy_points: str
    drawdown_high_label: str
    drawdown_midpoint_label: str
    drawdown_low_label: str
    strategy_max_drawdown: str
    spy_max_drawdown: str
    strategy_floor_reached: bool
    exposure_open_path: str
    exposure_empty_message: str | None
    exposure_invested_percent: str
    exposure_zero_sessions: int
    latest_open_exposure: str
    price_close_points: str
    price_sma200_points: str
    price_high_label: str
    price_midpoint_label: str
    price_low_label: str
    latest_close: str
    latest_sma200: str
    price_relationship: str
    risk_rv20_points: str
    risk_dd20_points: str
    risk_high_label: str
    risk_midpoint_label: str
    risk_low_label: str
    latest_rv20: str
    latest_dd20: str
    rv20_floor_y: str
    rv20_zero_bonus_y: str
    dd20_bonus_gate_y: str
    event_lock_reference_visible: bool
    event_lock_y: str
    leverage_raw_target_points: str
    leverage_effective_target_path: str
    latest_raw_target: str
    latest_effective_target: str
    effective_target_start_date: str | None
    empty_message: str | None


@dataclass(frozen=True, slots=True)
class RecommendationView(SerializableView):
    state: str
    headline: str
    quantity: int | None
    reason: str | None
    active_contract: str
    strategy_amount: str
    mes_mark: str
    mes_mark_at: str
    final_target: str
    achieved_leverage: str
    target_notional: str
    achieved_notional: str
    effective_at: str
    historical_quantity: int | None
    current_quantity: int | None
    quantity_difference: int | None


@dataclass(frozen=True, slots=True)
class PendingView(SerializableView):
    quantity: int
    effective_at: str
    target: str


@dataclass(frozen=True, slots=True)
class EventDocumentView(SerializableView):
    title: str
    publisher: str
    source_class: str
    published_at: str
    retrieved_at: str
    url: str | None


@dataclass(frozen=True, slots=True)
class CriterionView(SerializableView):
    criterion: str
    status: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class EventView(SerializableView):
    state: str
    event_type: str
    window_start: str
    window_end: str
    drawdown: str
    price_gate: str
    recovery: str
    detector: str
    verifier: str
    documents: tuple[EventDocumentView, ...]
    criteria: tuple[CriterionView, ...]
    missing_evidence: tuple[str, ...]
    contradictions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SettingsView(SerializableView):
    strategy_amount: str
    current_mes_quantity: str
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    openai_key_configured: bool
    detector_model: str
    verifier_model: str


@dataclass(frozen=True, slots=True)
class HistoryRow(SerializableView):
    calculated_at: str
    quantity: str
    target: str
    achieved: str
    status: str


@dataclass(frozen=True, slots=True)
class DashboardView(SerializableView):
    recommendation: RecommendationView
    pending: PendingView | None
    strategy_chart: StrategyChartView
    calculation: tuple[MetricRow, ...]
    event: EventView
    statuses: tuple[StatusCell, ...]
    settings: SettingsView
    history: tuple[HistoryRow, ...]
    csrf_token: str = ""


@dataclass(frozen=True, slots=True)
class OperationalOverrides:
    """Ephemeral process state that must not be inferred from stored history."""

    ibkr_connection: str | None = None
    next_valid_signal: datetime | None = None
    next_execution_window: datetime | None = None


def _plot_coordinate(value: Decimal, *, low: Decimal, high: Decimal) -> Decimal:
    return (high - value) * Decimal(100) / (high - low)


def _padded_domain(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    data_low = min(values)
    data_high = max(values)
    spread = data_high - data_low
    padding = (
        spread * Decimal("0.08")
        if spread
        else max(abs(data_high) * Decimal("0.01"), Decimal("0.01"))
    )
    low = data_low - padding
    high = data_high + padding
    return low, high, (low + high) / Decimal(2)


def _polyline_points(
    values: list[Decimal], *, x_values: list[Decimal], low: Decimal, high: Decimal
) -> str:
    return " ".join(
        f"{x:.2f},{_plot_coordinate(value, low=low, high=high):.2f}"
        for x, value in zip(x_values, values, strict=True)
    )


def _step_path(
    values: list[Decimal], *, x_values: list[Decimal], low: Decimal, high: Decimal
) -> str:
    commands: list[str] = []
    for x, value in zip(x_values, values, strict=True):
        y = _plot_coordinate(value, low=low, high=high)
        if not commands:
            commands.append(f"M {x:.2f},{y:.2f}")
            continue
        commands.append(f"H {x:.2f} V {y:.2f}")
    return " ".join(commands)


def _logical_event_cap_changes(
    transitions: list[EventStateRecord], calendar: XnysCalendar
) -> list[tuple[datetime, Decimal, int]]:
    changes: list[tuple[datetime, Decimal, int]] = []
    zero_states = {"EVENT_LOCKED", "RECOVERY_TRACKING", "RECOVERY_CONFIRMED"}
    for order, transition in enumerate(transitions):
        if transition.state in zero_states:
            session_date = transition.lock_session or transition.last_evaluated_session
            if session_date is None or not calendar.is_session(session_date):
                raise PerformanceUnavailable(
                    f"{transition.state} has no valid evaluated market session"
                )
            effective_at = calendar.session_close(session_date)
            cap = Decimal(0)
        elif transition.state == "NO_EVENT" and transition.release_at is not None:
            cap = MAX_TARGET
            effective_at = transition.release_at
        else:
            # NO_EVENT, review, and active-window transitions leave the normal
            # 4.85 cap unchanged. Only an explicit recovery release can lift a
            # previously persisted zero cap.
            continue
        changes.append((effective_at, cap, order))
    return sorted(changes, key=lambda item: (item[0], item[2]))


def _sessions_are_contiguous(dates: list[date], calendar: XnysCalendar) -> bool:
    for previous, current in zip(dates, dates[1:], strict=False):
        candidate = previous + timedelta(days=1)
        while candidate < current:
            if calendar.is_session(candidate):
                return False
            candidate += timedelta(days=1)
    return True


def _performance_series(
    visible: list[tuple[SignalSnapshotRecord, Decimal, Decimal, Decimal]],
    bars: list[SpyBarRecord],
    *,
    calendar: XnysCalendar,
) -> PerformanceSeries:
    bar_by_session = {item.session_date: item for item in bars}
    dates = [item[0].session_date for item in visible]
    if not _sessions_are_contiguous(dates, calendar):
        raise PerformanceUnavailable("completed strategy sessions contain a gap")
    try:
        selected_bars = [bar_by_session[session_date] for session_date in dates]
    except KeyError as exc:
        raise PerformanceUnavailable(
            f"completed SPY OHLC is missing for {exc.args[0].isoformat()}"
        ) from exc
    for (snapshot, _, _, _), bar in zip(visible, selected_bars, strict=True):
        if snapshot.spy_close != bar.close:
            raise PerformanceUnavailable(
                f"SPY bar and strategy calculation disagree for {snapshot.session_date.isoformat()}"
            )
    return calculate_performance(
        [
            SpyOhlcBar(
                session_date=item.session_date,
                open_price=item.open,
                high_price=item.high,
                low_price=item.low,
                close_price=item.close,
            )
            for item in selected_bars
        ],
        [
            ExposureSchedulePoint(
                session_date=snapshot.session_date,
                open_exposure=open_exposure,
                close_cap=close_cap,
            )
            for snapshot, open_exposure, close_cap, _ in visible
        ],
    )


def _empty_strategy_chart(
    canonical: list[SignalSnapshotRecord], *, message: str
) -> StrategyChartView:
    latest = canonical[-1] if canonical else None
    return StrategyChartView(
        available=False,
        performance_available=False,
        performance_empty_message="Historical rule replay needs two completed sessions.",
        session_count=len(canonical),
        start_date="—",
        end_date="—",
        performance_strategy_points="",
        performance_spy_points="",
        performance_high_label="—",
        performance_midpoint_label="—",
        performance_low_label="—",
        latest_strategy_value="—",
        latest_spy_value="—",
        strategy_total_return="—",
        spy_total_return="—",
        drawdown_strategy_points="",
        drawdown_spy_points="",
        drawdown_high_label="—",
        drawdown_midpoint_label="—",
        drawdown_low_label="—",
        strategy_max_drawdown="—",
        spy_max_drawdown="—",
        strategy_floor_reached=False,
        exposure_open_path="",
        exposure_empty_message="Historical exposure needs complete strategy sessions.",
        exposure_invested_percent="—",
        exposure_zero_sessions=0,
        latest_open_exposure="—",
        price_close_points="",
        price_sma200_points="",
        price_high_label="—",
        price_midpoint_label="—",
        price_low_label="—",
        latest_close=_price(latest.spy_close) if latest else "—",
        latest_sma200=_price(latest.sma200) if latest else "—",
        price_relationship="",
        risk_rv20_points="",
        risk_dd20_points="",
        risk_high_label="—",
        risk_midpoint_label="—",
        risk_low_label="—",
        latest_rv20=_percentage(latest.rv20) if latest else "—",
        latest_dd20=_percentage(latest.dd20) if latest else "—",
        rv20_floor_y="",
        rv20_zero_bonus_y="",
        dd20_bonus_gate_y="",
        event_lock_reference_visible=False,
        event_lock_y="",
        leverage_raw_target_points="",
        leverage_effective_target_path="",
        latest_raw_target=_multiple(latest.raw_target) if latest else "—",
        latest_effective_target="—",
        effective_target_start_date=None,
        empty_message=message,
    )


def _strategy_chart_view(
    snapshots: list[SignalSnapshotRecord],
    transitions: list[EventStateRecord],
    bars: list[SpyBarRecord],
    *,
    calendar: XnysCalendar,
) -> StrategyChartView:
    canonical = [item for item in snapshots if calendar.is_session(item.session_date)]
    if len(canonical) < 2:
        return _empty_strategy_chart(
            canonical,
            message=f"Waiting for two completed strategy calculations ({len(canonical)} stored).",
        )

    schedule_changes: list[tuple[datetime, Decimal]] = []
    for snapshot in canonical:
        window = signal_window_for(snapshot.session_date, calendar)
        if window is not None:
            schedule_changes.append((window.effective_at, snapshot.raw_target))

    try:
        event_changes = _logical_event_cap_changes(transitions, calendar)
    except PerformanceUnavailable as exc:
        return _empty_strategy_chart(
            canonical,
            message=f"Historical strategy replay unavailable: {exc}.",
        )
    schedule_index = 0
    event_index = 0
    scheduled_target: Decimal | None = None
    effective_event_cap = MAX_TARGET
    aligned: list[tuple[SignalSnapshotRecord, Decimal | None, Decimal, Decimal | None]] = []
    for snapshot in canonical:
        session_open = calendar.session_open(snapshot.session_date)
        session_close = calendar.session_close(snapshot.session_date)
        while (
            schedule_index < len(schedule_changes)
            and schedule_changes[schedule_index][0] <= session_open
        ):
            scheduled_target = schedule_changes[schedule_index][1]
            schedule_index += 1
        while event_index < len(event_changes) and event_changes[event_index][0] <= session_open:
            effective_event_cap = event_changes[event_index][1]
            event_index += 1
        open_exposure = (
            None
            if scheduled_target is None
            else min(scheduled_target, effective_event_cap, MAX_TARGET)
        )
        while event_index < len(event_changes) and event_changes[event_index][0] <= session_close:
            effective_event_cap = event_changes[event_index][1]
            event_index += 1
        close_exposure = (
            None
            if scheduled_target is None
            else min(scheduled_target, effective_event_cap, MAX_TARGET)
        )
        aligned.append((snapshot, open_exposure, effective_event_cap, close_exposure))

    first_known = next((index for index, item in enumerate(aligned) if item[1] is not None), None)
    if first_known is None or len(aligned) - first_known < 2:
        return _empty_strategy_chart(
            canonical,
            message="Waiting for two sessions with a reconstructable effective target.",
        )
    known = aligned[first_known:]
    if any(item[1] is None or item[3] is None for item in known):
        return _empty_strategy_chart(
            canonical,
            message="Effective exposure history contains a gap.",
        )
    resolved = [
        (snapshot, cast(Decimal, open_exposure), close_cap, cast(Decimal, close_exposure))
        for snapshot, open_exposure, close_cap, close_exposure in known
    ]
    exposure_problem = None
    if not _sessions_are_contiguous([item[0].session_date for item in resolved], calendar):
        exposure_problem = (
            "Historical exposure unavailable: completed strategy sessions contain a gap."
        )
    visible = resolved[-252:]
    x_step = Decimal(1000) / Decimal(len(visible) - 1)
    x_values = [x_step * index for index in range(len(visible))]

    price_values = [item[0].spy_close for item in visible] + [item[0].sma200 for item in visible]
    price_low, price_high, price_midpoint = _padded_domain(price_values)
    close_values = [item[0].spy_close for item in visible]
    sma_values = [item[0].sma200 for item in visible]

    latest_snapshot = visible[-1][0]
    distance = latest_snapshot.spy_close / latest_snapshot.sma200 - Decimal(1)
    if distance > 0:
        relationship = f"SPY is {_percentage(distance)} above its SMA200."
    elif distance < 0:
        relationship = f"SPY is {_percentage(abs(distance))} below its SMA200."
    else:
        relationship = "SPY is exactly at its SMA200."

    event_lock_reference_visible = any(item[2] == 0 for item in visible) or bool(
        transitions
        and transitions[-1].state
        in {"WINDOW_ACTIVE", "EVENT_LOCKED", "RECOVERY_TRACKING", "RECOVERY_CONFIRMED"}
    )
    rv20_values = [item[0].rv20 for item in visible]
    dd20_values = [item[0].dd20 for item in visible]
    risk_domain_values = (
        rv20_values
        + dd20_values
        + [
            Decimal("0.08"),
            Decimal("0.20"),
            Decimal("-0.01"),
        ]
    )
    if event_lock_reference_visible:
        risk_domain_values.append(Decimal("-0.04"))
    risk_low, risk_high, risk_midpoint = _padded_domain(risk_domain_values)

    raw_targets = [item[0].raw_target for item in visible]
    effective_targets = [item[3] for item in visible]
    open_exposures = [item[1] for item in visible]
    effective_start = visible[0][0].session_date.isoformat()
    latest_effective = effective_targets[-1]

    performance: PerformanceSeries | None = None
    performance_problem: str | None = None
    try:
        performance = _performance_series(visible, bars, calendar=calendar)
    except PerformanceUnavailable as exc:
        performance_problem = f"Historical rule replay unavailable: {exc}."

    if performance is not None:
        performance_values = [point.strategy_value for point in performance.points] + [
            point.spy_value for point in performance.points
        ]
        performance_low, performance_high, performance_midpoint = _padded_domain(performance_values)
        strategy_values = [point.strategy_value for point in performance.points]
        spy_values = [point.spy_value for point in performance.points]
        strategy_drawdowns = [point.strategy_drawdown for point in performance.points]
        spy_drawdowns = [point.spy_drawdown for point in performance.points]
        drawdown_low = min(strategy_drawdowns + spy_drawdowns)
        if drawdown_low == 0:
            drawdown_low = Decimal("-0.01")
        else:
            drawdown_low *= Decimal("1.08")
        drawdown_high = Decimal(0)
        drawdown_midpoint = drawdown_low / Decimal(2)
        latest_performance = performance.points[-1]
        performance_strategy_points = _polyline_points(
            strategy_values,
            x_values=x_values,
            low=performance_low,
            high=performance_high,
        )
        performance_spy_points = _polyline_points(
            spy_values,
            x_values=x_values,
            low=performance_low,
            high=performance_high,
        )
        drawdown_strategy_points = _polyline_points(
            strategy_drawdowns,
            x_values=x_values,
            low=drawdown_low,
            high=drawdown_high,
        )
        drawdown_spy_points = _polyline_points(
            spy_drawdowns,
            x_values=x_values,
            low=drawdown_low,
            high=drawdown_high,
        )
    else:
        performance_low = performance_high = performance_midpoint = Decimal(0)
        drawdown_low = drawdown_high = drawdown_midpoint = Decimal(0)
        latest_performance = None
        performance_strategy_points = performance_spy_points = ""
        drawdown_strategy_points = drawdown_spy_points = ""

    return StrategyChartView(
        available=True,
        performance_available=performance is not None,
        performance_empty_message=performance_problem,
        session_count=len(visible),
        start_date=visible[0][0].session_date.isoformat(),
        end_date=latest_snapshot.session_date.isoformat(),
        performance_strategy_points=performance_strategy_points,
        performance_spy_points=performance_spy_points,
        performance_high_label=_money(performance_high) if performance else "—",
        performance_midpoint_label=_money(performance_midpoint) if performance else "—",
        performance_low_label=_money(performance_low) if performance else "—",
        latest_strategy_value=_money(latest_performance.strategy_value)
        if latest_performance
        else "—",
        latest_spy_value=_money(latest_performance.spy_value) if latest_performance else "—",
        strategy_total_return=_percentage(
            latest_performance.strategy_value / Decimal(100) - Decimal(1)
        )
        if latest_performance
        else "—",
        spy_total_return=_percentage(latest_performance.spy_value / Decimal(100) - Decimal(1))
        if latest_performance
        else "—",
        drawdown_strategy_points=drawdown_strategy_points,
        drawdown_spy_points=drawdown_spy_points,
        drawdown_high_label=_percentage(drawdown_high) if performance else "—",
        drawdown_midpoint_label=_percentage(drawdown_midpoint) if performance else "—",
        drawdown_low_label=_percentage(drawdown_low) if performance else "—",
        strategy_max_drawdown=_percentage(performance.strategy_max_drawdown)
        if performance
        else "—",
        spy_max_drawdown=_percentage(performance.spy_max_drawdown) if performance else "—",
        strategy_floor_reached=bool(
            performance and any(point.strategy_at_floor for point in performance.points)
        ),
        exposure_open_path=(
            _step_path(
                open_exposures,
                x_values=x_values,
                low=Decimal(0),
                high=MAX_TARGET,
            )
            if exposure_problem is None
            else ""
        ),
        exposure_empty_message=exposure_problem,
        exposure_invested_percent=(
            _percentage(
                Decimal(sum(value > 0 for value in open_exposures)) / Decimal(len(open_exposures))
            )
            if exposure_problem is None
            else "—"
        ),
        exposure_zero_sessions=(
            sum(value == 0 for value in open_exposures) if exposure_problem is None else 0
        ),
        latest_open_exposure=(_multiple(open_exposures[-1]) if exposure_problem is None else "—"),
        price_close_points=_polyline_points(
            close_values, x_values=x_values, low=price_low, high=price_high
        ),
        price_sma200_points=_polyline_points(
            sma_values, x_values=x_values, low=price_low, high=price_high
        ),
        price_high_label=_price(price_high),
        price_midpoint_label=_price(price_midpoint),
        price_low_label=_price(price_low),
        latest_close=_price(latest_snapshot.spy_close),
        latest_sma200=_price(latest_snapshot.sma200),
        price_relationship=relationship,
        risk_rv20_points=_polyline_points(
            rv20_values, x_values=x_values, low=risk_low, high=risk_high
        ),
        risk_dd20_points=_polyline_points(
            dd20_values, x_values=x_values, low=risk_low, high=risk_high
        ),
        risk_high_label=_percentage(risk_high),
        risk_midpoint_label=_percentage(risk_midpoint),
        risk_low_label=_percentage(risk_low),
        latest_rv20=_percentage(latest_snapshot.rv20),
        latest_dd20=_percentage(latest_snapshot.dd20),
        rv20_floor_y=f"{_plot_coordinate(Decimal('0.08'), low=risk_low, high=risk_high):.2f}",
        rv20_zero_bonus_y=(
            f"{_plot_coordinate(Decimal('0.20'), low=risk_low, high=risk_high):.2f}"
        ),
        dd20_bonus_gate_y=(
            f"{_plot_coordinate(Decimal('-0.01'), low=risk_low, high=risk_high):.2f}"
        ),
        event_lock_reference_visible=event_lock_reference_visible,
        event_lock_y=(
            f"{_plot_coordinate(Decimal('-0.04'), low=risk_low, high=risk_high):.2f}"
            if event_lock_reference_visible
            else ""
        ),
        leverage_raw_target_points=_polyline_points(
            raw_targets, x_values=x_values, low=Decimal(0), high=MAX_TARGET
        ),
        leverage_effective_target_path=_step_path(
            effective_targets, x_values=x_values, low=Decimal(0), high=MAX_TARGET
        ),
        latest_raw_target=_multiple(latest_snapshot.raw_target),
        latest_effective_target=_multiple(latest_effective),
        effective_target_start_date=effective_start,
        empty_message=None,
    )


def _recommendation_state(
    recommendation: RecommendationRecord | None,
    *,
    ibkr_connection: str | None,
    quote_received_at: datetime | None,
    market_data_type: int | None,
    now: datetime,
    quote_max_age: timedelta,
) -> str:
    if recommendation is None or recommendation.status == "UNAVAILABLE":
        return "UNAVAILABLE"
    if recommendation.status == "STALE":
        return "STALE"
    if ibkr_connection is not None and ibkr_connection.upper() != "LIVE":
        return "STALE"
    if market_data_type != 1:
        return "UNAVAILABLE"
    if quote_received_at is None or now - quote_received_at > quote_max_age:
        return "STALE"
    return "LIVE"


def _recommendation_view(
    record: RecommendationRecord | None,
    *,
    state: str,
    current_quantity: int | None,
) -> RecommendationView:
    is_live = state == "LIVE" and record is not None and record.quantity is not None
    reason = (
        None
        if is_live
        else (record.reason if record else "Required market data and settings are unavailable.")
    )
    quantity = record.quantity if is_live and record else None
    historical = record.quantity if not is_live and record and record.quantity is not None else None
    difference = None
    if quantity is not None and current_quantity is not None:
        difference = quantity - current_quantity
    return RecommendationView(
        state=state,
        headline=f"HOLD NOW: {quantity} MES" if is_live else "RECOMMENDATION UNAVAILABLE",
        quantity=quantity,
        reason=reason,
        active_contract=record.contract_local_symbol
        if record and record.contract_local_symbol
        else "—",
        strategy_amount=_money(record.strategy_amount_usd if record else None),
        mes_mark=_price(record.mes_mark if record else None),
        mes_mark_at=_display_time(record.mes_mark_at if record else None),
        final_target=_multiple(record.final_target if record else None),
        achieved_leverage=_multiple(record.achieved_leverage if record else None),
        target_notional=_money(record.target_notional if record else None),
        achieved_notional=_money(record.achieved_notional if record else None),
        effective_at=_display_time(record.target_effective_at if record else None),
        historical_quantity=historical,
        current_quantity=current_quantity,
        quantity_difference=difference,
    )


def build_dashboard_view(
    repository: Repository,
    *,
    now: datetime | None = None,
    quote_max_age: timedelta = timedelta(seconds=60),
    operational: OperationalOverrides | None = None,
    csrf_token: str = "",
) -> DashboardView:
    """Build a deterministic snapshot for HTML rendering or JSON serialization."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    operational = operational or OperationalOverrides()
    settings = repository.get_settings()
    recommendation = repository.latest_recommendation()
    pending_record = repository.latest_recommendation(pending=True)
    snapshot = repository.latest_signal_snapshot()
    strategy_chart = _strategy_chart_view(
        repository.latest_signal_snapshots(1000),
        repository.event_state_transition_history(2000),
        repository.latest_spy_bars(1000),
        calendar=XNYS,
    )
    quote = repository.latest_mes_quote()
    contract = repository.latest_contract()
    current_target = repository.current_scheduled_target(now)
    pending_target = repository.pending_scheduled_target(now)
    event_state = repository.latest_event_state()
    event_case = None
    if event_state and event_state.event_case_id is not None:
        event_case = repository.event_case(event_state.event_case_id)
    elif event_state is None:
        event_case = repository.latest_event_case()

    recommendation_state = _recommendation_state(
        recommendation,
        ibkr_connection=operational.ibkr_connection,
        quote_received_at=quote.received_at if quote else None,
        market_data_type=quote.market_data_type if quote else None,
        now=now,
        quote_max_age=quote_max_age,
    )
    recommendation_view = _recommendation_view(
        recommendation,
        state=recommendation_state,
        current_quantity=settings.current_mes_quantity,
    )

    pending = None
    if (
        recommendation_state == "LIVE"
        and pending_record is not None
        and pending_record.status == "LIVE"
        and pending_record.quantity is not None
        and pending_record.target_effective_at is not None
        and pending_record.target_effective_at > now
        and pending_target is not None
        and pending_record.target_effective_at == pending_target.effective_at
        and pending_record.scheduled_target == pending_target.target
    ):
        pending = PendingView(
            quantity=pending_record.quantity,
            effective_at=_display_time(pending_record.target_effective_at),
            target=_multiple(pending_record.final_target),
        )

    effective_event_cap = event_state.event_cap if event_state else Decimal("4.85")
    calculation = (
        MetricRow("SPY close", _price(snapshot.spy_close if snapshot else None)),
        MetricRow("SMA200", _price(snapshot.sma200 if snapshot else None)),
        MetricRow(
            "Distance above SMA200",
            _percentage(snapshot.distance_above_sma200 if snapshot else None),
        ),
        MetricRow("RV20", _percentage(snapshot.rv20 if snapshot else None)),
        MetricRow("DD20", _percentage(snapshot.dd20 if snapshot else None)),
        MetricRow("Base", _multiple(snapshot.base if snapshot else None)),
        MetricRow("Bonus", _multiple(snapshot.bonus if snapshot else None)),
        MetricRow("Volatility cap", _multiple(snapshot.volatility_cap if snapshot else None)),
        MetricRow("Raw target", _multiple(snapshot.raw_target if snapshot else None)),
        MetricRow(
            "Current scheduled target", _multiple(current_target.target if current_target else None)
        ),
        MetricRow("Event cap", _multiple(effective_event_cap)),
        MetricRow(
            "Final target", _multiple(recommendation.final_target if recommendation else None)
        ),
    )

    documents: tuple[EventDocumentView, ...] = ()
    criteria: tuple[CriterionView, ...] = ()
    missing: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    detector = repository.latest_event_model_run(
        "DETECTOR", event_case_id=event_case.id if event_case else None
    )
    verifier = repository.latest_event_model_run(
        "VERIFIER", event_case_id=event_case.id if event_case else None
    )
    if event_case and event_case.id is not None:
        document_records = repository.event_documents(event_case.id)
        evidence_records = repository.event_evidence(event_case.id)
    else:
        model_runs = [run for run in (detector, verifier) if run is not None and run.id is not None]
        document_records = []
        evidence_records = []
        for run in model_runs:
            assert run.id is not None
            document_records.extend(repository.event_documents_for_model_run(run.id))
            evidence_records.extend(repository.event_evidence_for_model_run(run.id))
    if document_records or evidence_records:
        document_records = list({item.id: item for item in document_records}.values())
        evidence_records = list({item.id: item for item in evidence_records}.values())
        documents = tuple(
            EventDocumentView(
                title=item.title,
                publisher=item.publisher,
                source_class=item.source_class,
                published_at=_display_time(item.published_at),
                retrieved_at=_display_time(item.retrieved_at),
                url=_safe_url(item.url),
            )
            for item in document_records
        )
        criteria = tuple(
            CriterionView(
                criterion=item.criterion_key.replace("_", " ").title(),
                status="CONTRADICTION"
                if item.contradiction
                else ("SUPPORTED" if item.supported else "MISSING"),
                excerpt=item.excerpt or "No evidence excerpt stored.",
            )
            for item in evidence_records
        )
        missing = tuple(
            item.criterion_key.replace("_", " ").title()
            for item in evidence_records
            if not item.supported and not item.contradiction
        )
        contradictions = tuple(
            item.criterion_key.replace("_", " ").title()
            for item in evidence_records
            if item.contradiction
        )

    state_name = event_state.state if event_state else "NO_EVENT"
    if state_name in {"EVENT_LOCKED", "RECOVERY_TRACKING", "RECOVERY_CONFIRMED"}:
        gate = "TRIGGERED"
    elif state_name == "WINDOW_ACTIVE":
        gate = "NOT TRIGGERED"
    else:
        gate = "NOT APPLICABLE"
    event_view = EventView(
        state=state_name,
        event_type=event_case.event_type.replace("_", " ").title() if event_case else "None",
        window_start=_display_time(event_case.window_start if event_case else None),
        window_end=_display_time(event_case.window_end if event_case else None),
        drawdown=_percentage(event_state.event_drawdown if event_state else None),
        price_gate=gate,
        recovery=(
            f"{event_state.recovery_sessions}/3 sessions at or above "
            f"{_price(event_state.recovery_level)}"
            if event_state and event_state.recovery_level is not None
            else "Not active"
        ),
        detector=detector.result if detector else "NOT RUN",
        verifier=verifier.result if verifier else "NOT RUN",
        documents=documents,
        criteria=criteria,
        missing_evidence=missing,
        contradictions=contradictions,
    )

    quote_age = now - quote.received_at if quote else None
    if quote is None:
        quote_status, quote_value = "UNAVAILABLE", "No quote"
    elif quote.market_data_type != 1:
        quote_status, quote_value = "NOT_ENTITLED", "Not live"
    elif quote_age is not None and quote_age > quote_max_age:
        quote_status, quote_value = "STALE", f"{int(quote_age.total_seconds())}s old"
    else:
        assert quote_age is not None
        quote_status, quote_value = "LIVE", f"{max(0, int(quote_age.total_seconds()))}s old"

    if quote is None or quote.market_data_type is None:
        market_type_value, market_type_state = "Unknown", "UNAVAILABLE"
    else:
        market_names = {1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed frozen"}
        market_type_value = market_names.get(
            quote.market_data_type, f"Type {quote.market_data_type}"
        )
        market_type_state = "LIVE" if quote.market_data_type == 1 else "NOT_ENTITLED"

    inferred_connection = "LIVE" if quote_status == "LIVE" else "UNAVAILABLE"
    connection_state = (operational.ibkr_connection or inferred_connection).upper()
    statuses = (
        StatusCell("IBKR connection", connection_state, connection_state),
        StatusCell(
            "SPY bar date",
            snapshot.session_date.isoformat() if snapshot else "No completed bar",
            "LIVE" if snapshot else "UNAVAILABLE",
        ),
        StatusCell("MES quote freshness", quote_value, quote_status),
        StatusCell("MES market-data type", market_type_value, market_type_state),
        StatusCell(
            "Active MES contract",
            contract.local_symbol if contract else "Unresolved",
            "LIVE" if contract else "UNAVAILABLE",
        ),
        StatusCell(
            "Last calculation",
            _display_time(snapshot.calculated_at if snapshot else None),
            "LIVE" if snapshot else "UNAVAILABLE",
        ),
        StatusCell(
            "Next signal",
            _display_time(operational.next_valid_signal)
            if operational.next_valid_signal
            else (
                pending_target.signal_session_date.isoformat() if pending_target else "Unscheduled"
            ),
            "LIVE" if operational.next_valid_signal or pending_target else "UNAVAILABLE",
        ),
        StatusCell(
            "Next execution window",
            _display_time(
                operational.next_execution_window
                or (pending_target.effective_at if pending_target else None)
            ),
            "LIVE" if operational.next_execution_window or pending_target else "UNAVAILABLE",
        ),
    )

    history = tuple(
        HistoryRow(
            calculated_at=_display_time(item.calculated_at),
            quantity=f"{item.quantity} MES" if item.quantity is not None else "—",
            target=_multiple(item.final_target),
            achieved=_multiple(item.achieved_leverage),
            status=item.status,
        )
        for item in repository.recommendation_history(10)
    )

    return DashboardView(
        recommendation=recommendation_view,
        pending=pending,
        strategy_chart=strategy_chart,
        calculation=calculation,
        event=event_view,
        statuses=statuses,
        settings=SettingsView(
            strategy_amount=""
            if settings.strategy_amount_usd is None
            else format(settings.strategy_amount_usd, "f"),
            current_mes_quantity=""
            if settings.current_mes_quantity is None
            else str(settings.current_mes_quantity),
            ibkr_host=settings.ibkr_host,
            ibkr_port=settings.ibkr_port,
            ibkr_client_id=settings.ibkr_client_id,
            openai_key_configured=settings.openai_api_key_ciphertext is not None,
            detector_model=settings.detector_model,
            verifier_model=settings.verifier_model,
        ),
        history=history,
        csrf_token=csrf_token,
    )
