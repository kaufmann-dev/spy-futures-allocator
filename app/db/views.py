"""Read-only, JSON-safe views for the allocator API and dashboard."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.domain.errors import PerformanceUnavailable
from app.domain.performance import (
    ExposureSchedulePoint,
    PerformanceSeries,
    SpyOhlcBar,
    calculate_performance,
)
from app.integrations.ibkr import active_futures_expiry
from app.services.strategy_config import (
    strategy_config_fingerprint,
    strategy_config_from_settings,
)

from .models import (
    AppSettings,
    ContractRecord,
    FuturesQuoteRecord,
    RecommendationRecord,
    SpyBarRecord,
    StrategySnapshotRecord,
)
from .repository import Repository

VIENNA = ZoneInfo("Europe/Vienna")
NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal(0)
ONE_HUNDRED = Decimal(100)
FUTURE_TOLERANCE = timedelta(seconds=5)


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
    return "—" if value is None else f"{value * ONE_HUNDRED:.2f}%"


def _input_decimal(value: Decimal) -> str:
    return format(value, "f")


def _input_percentage(value: Decimal) -> str:
    return format(value * ONE_HUNDRED, "f")


def _iso_api(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    empty_message: str | None
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
    exposure_path: str
    exposure_high_label: str
    exposure_midpoint_label: str
    latest_exposure: str
    risk_on_percent: str
    risk_off_sessions: int
    price_close_points: str
    price_fast_sma_points: str
    price_slow_sma_points: str
    price_high_label: str
    price_midpoint_label: str
    price_low_label: str
    latest_close: str
    latest_fast_sma: str
    latest_slow_sma: str
    fast_sma_label: str
    slow_sma_label: str
    price_relationship: str
    risk_rv_points: str
    risk_threshold_y: str
    risk_high_label: str
    risk_midpoint_label: str
    risk_low_label: str
    latest_rv: str
    rv_label: str
    rv_threshold_label: str


@dataclass(frozen=True, slots=True)
class RecommendationView(SerializableView):
    state: str
    headline: str
    reason: str | None
    historical_allocation: str | None
    es_contract: str
    mes_contract: str
    strategy_amount: str
    es_mark: str
    es_mark_at: str
    mes_mark: str
    mes_mark_at: str
    target_exposure: str
    achieved_exposure: str
    target_notional: str
    achieved_notional: str
    effective_at: str
    current_es_quantity: str
    current_mes_quantity: str
    es_difference: str
    mes_difference: str


@dataclass(frozen=True, slots=True)
class PendingView(SerializableView):
    headline: str
    effective_at: str
    target_exposure: str


@dataclass(frozen=True, slots=True)
class SettingsView(SerializableView):
    strategy_amount: str
    current_es_quantity: str
    current_mes_quantity: str
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    fast_sma_sessions: int
    slow_sma_sessions: int
    rebound_percent: str
    rv_window_sessions: int
    annualization_sessions: int
    rv_threshold_percent: str
    normal_exposure: str
    high_vol_exposure: str
    review_weekdays: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HistoryRow(SerializableView):
    calculated_at: str
    allocation: str
    target: str
    achieved: str
    status: str


@dataclass(frozen=True, slots=True)
class DashboardView(SerializableView):
    recommendation: RecommendationView
    pending: PendingView | None
    strategy_chart: StrategyChartView
    calculation: tuple[MetricRow, ...]
    statuses: tuple[StatusCell, ...]
    settings: SettingsView
    history: tuple[HistoryRow, ...]
    csrf_token: str = ""


@dataclass(frozen=True, slots=True)
class OperationalOverrides:
    """Ephemeral runtime state that must not be inferred from persisted history."""

    ibkr_connection: str | None = None
    config_fingerprint: str | None = None
    next_regime_evaluation: datetime | None = None
    next_volatility_review: datetime | None = None
    next_market_open: datetime | None = None


def _plot_coordinate(value: Decimal, *, low: Decimal, high: Decimal) -> Decimal:
    return (high - value) * ONE_HUNDRED / (high - low)


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


def _x_values(count: int) -> list[Decimal]:
    denominator = Decimal(count - 1)
    return [Decimal(index) * Decimal(1000) / denominator for index in range(count)]


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
        else:
            commands.append(f"H {x:.2f} V {y:.2f}")
    return " ".join(commands)


def _allocation(record: RecommendationRecord | None) -> str | None:
    if record is None or record.es_quantity is None or record.mes_quantity is None:
        return None
    return f"{record.es_quantity} ES + {record.mes_quantity} MES"


def _quantity(value: int | None) -> str:
    return "—" if value is None else str(value)


def _difference(target: int | None, current: int | None, *, live: bool) -> str:
    if not live or target is None or current is None:
        return "—"
    difference = target - current
    return f"{difference:+d}" if difference else "0"


def _contract_problem(
    es: ContractRecord | None,
    mes: ContractRecord | None,
    *,
    now: datetime,
) -> str | None:
    if es is None or mes is None:
        return "Both ES and MES contracts must be resolved."
    if es.multiplier != Decimal(50) or mes.multiplier != Decimal(5):
        return "The resolved ES or MES contract has an unexpected multiplier."
    if es.last_trade_date != mes.last_trade_date:
        return "The resolved ES and MES contracts do not share an expiry."
    expected_expiry = active_futures_expiry(now.astimezone(NEW_YORK).date())
    if es.last_trade_date[:6] != expected_expiry or mes.last_trade_date[:6] != expected_expiry:
        return "The resolved ES and MES contracts are not the active quarterly expiry."
    return None


def _market_data_name(market_data_type: int | None) -> str:
    names = {1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed frozen"}
    if market_data_type is None:
        return "Unknown"
    return names.get(market_data_type, f"Type {market_data_type}")


def _quote_check(
    quote: FuturesQuoteRecord | None,
    contract: ContractRecord | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> tuple[str, str, str | None]:
    symbol = contract.symbol if contract is not None else "Futures"
    if quote is None:
        return "UNAVAILABLE", "No quote", f"A {symbol} quote is unavailable."
    if contract is None or quote.con_id != contract.con_id:
        return "UNAVAILABLE", "Wrong contract", f"The {quote.symbol} quote is for another contract."
    if quote.market_data_type != 1:
        data_name = _market_data_name(quote.market_data_type)
        return "NOT_ENTITLED", data_name, f"The {quote.symbol} quote is not live market data."
    if quote.mark is None or quote.quoted_at is None:
        return "UNAVAILABLE", "No live mark", f"The {quote.symbol} live mark is unavailable."
    if quote.quoted_at > now + FUTURE_TOLERANCE or quote.received_at > now + FUTURE_TOLERANCE:
        return "UNAVAILABLE", "Future timestamp", f"The {quote.symbol} quote timestamp is invalid."
    freshness_at = min(quote.quoted_at, quote.received_at)
    age = now - freshness_at
    age_seconds = max(0, int(age.total_seconds()))
    if age > max_age:
        return "STALE", f"{age_seconds}s old", f"The {quote.symbol} quote is stale."
    return "LIVE", f"{age_seconds}s old", None


def _required_recommendation_values(record: RecommendationRecord) -> bool:
    return all(
        value is not None
        for value in (
            record.es_quantity,
            record.mes_quantity,
            record.es_contract_con_id,
            record.es_contract_local_symbol,
            record.es_mark,
            record.es_mark_at,
            record.es_multiplier,
            record.mes_contract_con_id,
            record.mes_contract_local_symbol,
            record.mes_mark,
            record.mes_mark_at,
            record.mes_multiplier,
            record.strategy_amount_usd,
            record.target_exposure,
            record.target_notional,
            record.achieved_notional,
            record.achieved_exposure,
            record.target_effective_at,
        )
    )


def _live_decision(
    record: RecommendationRecord | None,
    *,
    current_fingerprint: str,
    connection_state: str,
    now: datetime,
    quote_max_age: timedelta,
    es_contract: ContractRecord | None,
    mes_contract: ContractRecord | None,
    es_quote: FuturesQuoteRecord | None,
    mes_quote: FuturesQuoteRecord | None,
    es_quote_check: tuple[str, str, str | None],
    mes_quote_check: tuple[str, str, str | None],
) -> tuple[str, str | None]:
    if record is None:
        return "UNAVAILABLE", "No allocation has been calculated."
    if record.status != "LIVE":
        state = "STALE" if record.status == "STALE" else "UNAVAILABLE"
        return state, record.reason or "The saved allocation is not live."
    if record.config_fingerprint != current_fingerprint:
        return "UNAVAILABLE", "The saved allocation uses a different strategy configuration."
    if connection_state != "LIVE":
        return "STALE", "Interactive Brokers is disconnected."
    problem = _contract_problem(es_contract, mes_contract, now=now)
    if problem is not None:
        return "UNAVAILABLE", problem
    for state, _, quote_problem in (es_quote_check, mes_quote_check):
        if state != "LIVE":
            return ("STALE" if state == "STALE" else "UNAVAILABLE"), quote_problem
    if not _required_recommendation_values(record):
        return "UNAVAILABLE", "The saved allocation is incomplete."
    assert es_contract is not None and mes_contract is not None
    assert es_quote is not None and mes_quote is not None
    if (
        record.es_contract_con_id != es_contract.con_id
        or record.mes_contract_con_id != mes_contract.con_id
        or record.es_contract_local_symbol != es_contract.local_symbol
        or record.mes_contract_local_symbol != mes_contract.local_symbol
    ):
        return "UNAVAILABLE", "The saved allocation uses different futures contracts."
    if (
        record.es_multiplier != es_contract.multiplier
        or record.mes_multiplier != mes_contract.multiplier
    ):
        return "UNAVAILABLE", "The saved allocation uses unexpected contract multipliers."
    assert record.es_mark_at is not None and record.mes_mark_at is not None
    if (
        record.es_mark != es_quote.mark
        or record.mes_mark != mes_quote.mark
        or record.es_mark_at != es_quote.quoted_at
        or record.mes_mark_at != mes_quote.quoted_at
    ):
        return "UNAVAILABLE", "The saved allocation does not use the latest validated quotes."
    if (
        record.es_mark_at > now + FUTURE_TOLERANCE
        or record.mes_mark_at > now + FUTURE_TOLERANCE
    ):
        return "UNAVAILABLE", "The saved allocation has a future quote timestamp."
    if now - record.es_mark_at > quote_max_age or now - record.mes_mark_at > quote_max_age:
        return "STALE", "The quote used to size the saved allocation is stale."
    return "LIVE", record.reason


def _recommendation_view(
    record: RecommendationRecord | None,
    *,
    state: str,
    reason: str | None,
    settings: AppSettings,
) -> RecommendationView:
    allocation = _allocation(record)
    is_live = state == "LIVE" and allocation is not None
    return RecommendationView(
        state=state,
        headline=f"HOLD NOW: {allocation}" if is_live else "RECOMMENDATION UNAVAILABLE",
        reason=reason,
        historical_allocation=None if is_live else allocation,
        es_contract=(record.es_contract_local_symbol if record else None) or "—",
        mes_contract=(record.mes_contract_local_symbol if record else None) or "—",
        strategy_amount=_money(record.strategy_amount_usd if record else None),
        es_mark=_price(record.es_mark if record else None),
        es_mark_at=_display_time(record.es_mark_at if record else None),
        mes_mark=_price(record.mes_mark if record else None),
        mes_mark_at=_display_time(record.mes_mark_at if record else None),
        target_exposure=_multiple(record.target_exposure if record else None),
        achieved_exposure=_multiple(record.achieved_exposure if record else None),
        target_notional=_money(record.target_notional if record else None),
        achieved_notional=_money(record.achieved_notional if record else None),
        effective_at=_display_time(record.target_effective_at if record else None),
        current_es_quantity=_quantity(settings.current_es_quantity),
        current_mes_quantity=_quantity(settings.current_mes_quantity),
        es_difference=_difference(
            record.es_quantity if record else None,
            settings.current_es_quantity,
            live=is_live,
        ),
        mes_difference=_difference(
            record.mes_quantity if record else None,
            settings.current_mes_quantity,
            live=is_live,
        ),
    )


def _empty_strategy_chart(
    snapshots: list[StrategySnapshotRecord],
    settings: AppSettings,
    *,
    message: str,
) -> StrategyChartView:
    latest = snapshots[-1] if snapshots else None
    return StrategyChartView(
        available=False,
        empty_message=message,
        performance_available=False,
        performance_empty_message=message,
        session_count=len(snapshots),
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
        exposure_path="",
        exposure_high_label="—",
        exposure_midpoint_label="—",
        latest_exposure="—",
        risk_on_percent="—",
        risk_off_sessions=0,
        price_close_points="",
        price_fast_sma_points="",
        price_slow_sma_points="",
        price_high_label="—",
        price_midpoint_label="—",
        price_low_label="—",
        latest_close=_price(latest.spy_close if latest else None),
        latest_fast_sma=_price(latest.fast_sma if latest else None),
        latest_slow_sma=_price(latest.slow_sma if latest else None),
        fast_sma_label=f"SMA{settings.fast_sma_sessions}",
        slow_sma_label=f"SMA{settings.slow_sma_sessions}",
        price_relationship="",
        risk_rv_points="",
        risk_threshold_y="",
        risk_high_label="—",
        risk_midpoint_label="—",
        risk_low_label="—",
        latest_rv=_percentage(latest.rv if latest else None),
        rv_label=f"RV{settings.rv_window_sessions}",
        rv_threshold_label=_percentage(settings.rv_threshold),
    )


def _aligned_chart_data(
    repository: Repository,
    fingerprint: str,
) -> tuple[
    list[StrategySnapshotRecord],
    list[SpyBarRecord],
    list[Decimal],
]:
    stored = repository.strategy_snapshots(fingerprint, 253)
    visible = stored[-252:]
    if len(visible) < 2:
        raise PerformanceUnavailable(
            f"waiting for two completed strategy snapshots ({len(visible)} stored)"
        )
    bars_by_date = {bar.session_date: bar for bar in repository.latest_spy_bars(10_000)}
    try:
        bars = [bars_by_date[snapshot.session_date] for snapshot in visible]
    except KeyError as exc:
        raise PerformanceUnavailable(
            f"completed SPY OHLC is missing for {exc.args[0].isoformat()}"
        ) from exc
    for snapshot, bar in zip(visible, bars, strict=True):
        if snapshot.spy_close != bar.close:
            raise PerformanceUnavailable(
                f"SPY OHLC and strategy snapshot disagree for {snapshot.session_date.isoformat()}"
            )

    predecessor = stored[-253] if len(stored) > len(visible) else None
    open_exposures = [predecessor.target_exposure if predecessor is not None else ZERO]
    open_exposures.extend(snapshot.target_exposure for snapshot in visible[:-1])
    return visible, bars, open_exposures


def _performance_series(
    snapshots: list[StrategySnapshotRecord],
    bars: list[SpyBarRecord],
    open_exposures: list[Decimal],
) -> PerformanceSeries:
    return calculate_performance(
        [
            SpyOhlcBar(
                session_date=bar.session_date,
                open_price=bar.open,
                high_price=bar.high,
                low_price=bar.low,
                close_price=bar.close,
            )
            for bar in bars
        ],
        [
            ExposureSchedulePoint(snapshot.session_date, exposure)
            for snapshot, exposure in zip(snapshots, open_exposures, strict=True)
        ],
    )


def _strategy_chart_view(
    repository: Repository,
    fingerprint: str,
    settings: AppSettings,
) -> StrategyChartView:
    stored = repository.strategy_snapshots(fingerprint, 253)
    visible = stored[-252:]
    try:
        snapshots, bars, open_exposures = _aligned_chart_data(repository, fingerprint)
        performance = _performance_series(snapshots, bars, open_exposures)
    except PerformanceUnavailable as exc:
        return _empty_strategy_chart(
            visible,
            settings,
            message=f"Historical strategy replay unavailable: {exc}.",
        )

    count = len(snapshots)
    x_values = _x_values(count)
    performance_strategy = [point.strategy_value for point in performance.points]
    performance_spy = [point.spy_value for point in performance.points]
    performance_low, performance_high, performance_midpoint = _padded_domain(
        performance_strategy + performance_spy
    )
    strategy_drawdowns = [point.strategy_drawdown for point in performance.points]
    spy_drawdowns = [point.spy_drawdown for point in performance.points]
    drawdown_high = ZERO
    drawdown_low = min(strategy_drawdowns + spy_drawdowns)
    drawdown_low = drawdown_low * Decimal("1.08") if drawdown_low else Decimal("-0.01")
    drawdown_midpoint = (drawdown_high + drawdown_low) / Decimal(2)

    exposure_high = max(
        max(open_exposures), settings.normal_exposure, settings.high_vol_exposure, Decimal("0.01")
    )
    exposure_midpoint = exposure_high / Decimal(2)
    close_values = [snapshot.spy_close for snapshot in snapshots]
    fast_values = [snapshot.fast_sma for snapshot in snapshots]
    slow_values = [snapshot.slow_sma for snapshot in snapshots]
    price_low, price_high, price_midpoint = _padded_domain(
        close_values + fast_values + slow_values
    )
    rv_values = [snapshot.rv for snapshot in snapshots]
    risk_low = ZERO
    risk_high = max(max(rv_values), settings.rv_threshold, Decimal("0.01")) * Decimal("1.08")
    risk_midpoint = (risk_low + risk_high) / Decimal(2)

    latest = snapshots[-1]
    relationship_value = latest.fast_sma / latest.slow_sma - Decimal(1)
    if relationship_value > ZERO:
        relationship = (
            f"SMA{settings.fast_sma_sessions} is {_percentage(relationship_value)} above "
            f"SMA{settings.slow_sma_sessions}."
        )
    elif relationship_value < ZERO:
        relationship = (
            f"SMA{settings.fast_sma_sessions} is {_percentage(abs(relationship_value))} below "
            f"SMA{settings.slow_sma_sessions}."
        )
    else:
        relationship = (
            f"SMA{settings.fast_sma_sessions} equals SMA{settings.slow_sma_sessions}."
        )

    risk_off_sessions = sum(snapshot.regime_state == "RISK_OFF" for snapshot in snapshots)
    risk_on_fraction = Decimal(count - risk_off_sessions) / Decimal(count)
    latest_strategy = performance.points[-1].strategy_value
    latest_spy = performance.points[-1].spy_value
    return StrategyChartView(
        available=True,
        empty_message=None,
        performance_available=True,
        performance_empty_message=None,
        session_count=count,
        start_date=snapshots[0].session_date.isoformat(),
        end_date=latest.session_date.isoformat(),
        performance_strategy_points=_polyline_points(
            performance_strategy,
            x_values=x_values,
            low=performance_low,
            high=performance_high,
        ),
        performance_spy_points=_polyline_points(
            performance_spy,
            x_values=x_values,
            low=performance_low,
            high=performance_high,
        ),
        performance_high_label=_money(performance_high),
        performance_midpoint_label=_money(performance_midpoint),
        performance_low_label=_money(performance_low),
        latest_strategy_value=_money(latest_strategy),
        latest_spy_value=_money(latest_spy),
        strategy_total_return=_percentage(latest_strategy / ONE_HUNDRED - Decimal(1)),
        spy_total_return=_percentage(latest_spy / ONE_HUNDRED - Decimal(1)),
        drawdown_strategy_points=_polyline_points(
            strategy_drawdowns,
            x_values=x_values,
            low=drawdown_low,
            high=drawdown_high,
        ),
        drawdown_spy_points=_polyline_points(
            spy_drawdowns,
            x_values=x_values,
            low=drawdown_low,
            high=drawdown_high,
        ),
        drawdown_high_label=_percentage(drawdown_high),
        drawdown_midpoint_label=_percentage(drawdown_midpoint),
        drawdown_low_label=_percentage(drawdown_low),
        strategy_max_drawdown=_percentage(performance.strategy_max_drawdown),
        spy_max_drawdown=_percentage(performance.spy_max_drawdown),
        exposure_path=_step_path(
            open_exposures,
            x_values=x_values,
            low=ZERO,
            high=exposure_high,
        ),
        exposure_high_label=_multiple(exposure_high),
        exposure_midpoint_label=_multiple(exposure_midpoint),
        latest_exposure=_multiple(open_exposures[-1]),
        risk_on_percent=_percentage(risk_on_fraction),
        risk_off_sessions=risk_off_sessions,
        price_close_points=_polyline_points(
            close_values,
            x_values=x_values,
            low=price_low,
            high=price_high,
        ),
        price_fast_sma_points=_polyline_points(
            fast_values,
            x_values=x_values,
            low=price_low,
            high=price_high,
        ),
        price_slow_sma_points=_polyline_points(
            slow_values,
            x_values=x_values,
            low=price_low,
            high=price_high,
        ),
        price_high_label=_price(price_high),
        price_midpoint_label=_price(price_midpoint),
        price_low_label=_price(price_low),
        latest_close=_price(latest.spy_close),
        latest_fast_sma=_price(latest.fast_sma),
        latest_slow_sma=_price(latest.slow_sma),
        fast_sma_label=f"SMA{settings.fast_sma_sessions}",
        slow_sma_label=f"SMA{settings.slow_sma_sessions}",
        price_relationship=relationship,
        risk_rv_points=_polyline_points(
            rv_values,
            x_values=x_values,
            low=risk_low,
            high=risk_high,
        ),
        risk_threshold_y=(
            f"{_plot_coordinate(settings.rv_threshold, low=risk_low, high=risk_high):.2f}"
        ),
        risk_high_label=_percentage(risk_high),
        risk_midpoint_label=_percentage(risk_midpoint),
        risk_low_label=_percentage(risk_low),
        latest_rv=_percentage(latest.rv),
        rv_label=f"RV{settings.rv_window_sessions}",
        rv_threshold_label=_percentage(settings.rv_threshold),
    )


def _settings_view(settings: AppSettings) -> SettingsView:
    return SettingsView(
        strategy_amount=(
            ""
            if settings.strategy_amount_usd is None
            else _input_decimal(settings.strategy_amount_usd)
        ),
        current_es_quantity=(
            "" if settings.current_es_quantity is None else str(settings.current_es_quantity)
        ),
        current_mes_quantity=(
            "" if settings.current_mes_quantity is None else str(settings.current_mes_quantity)
        ),
        ibkr_host=settings.ibkr_host,
        ibkr_port=settings.ibkr_port,
        ibkr_client_id=settings.ibkr_client_id,
        fast_sma_sessions=settings.fast_sma_sessions,
        slow_sma_sessions=settings.slow_sma_sessions,
        rebound_percent=_input_percentage(settings.rebound_fraction),
        rv_window_sessions=settings.rv_window_sessions,
        annualization_sessions=settings.annualization_sessions,
        rv_threshold_percent=_input_percentage(settings.rv_threshold),
        normal_exposure=_input_decimal(settings.normal_exposure),
        high_vol_exposure=_input_decimal(settings.high_vol_exposure),
        review_weekdays=settings.review_weekdays,
    )


def _schedule_status(label: str, value: datetime | None) -> StatusCell:
    return StatusCell(
        label,
        _display_time(value) if value else "Unscheduled",
        "LIVE" if value else "UNAVAILABLE",
    )


def build_dashboard_view(
    repository: Repository,
    *,
    now: datetime | None = None,
    quote_max_age: timedelta = timedelta(seconds=60),
    operational: OperationalOverrides | None = None,
    csrf_token: str = "",
) -> DashboardView:
    """Build one deterministic HTML/API snapshot and fail closed on unsafe live state."""

    selected_now = now or datetime.now(UTC)
    if selected_now.tzinfo is None or selected_now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    selected_now = selected_now.astimezone(UTC)
    operational = operational or OperationalOverrides()
    settings = repository.get_settings()
    computed_fingerprint = strategy_config_fingerprint(strategy_config_from_settings(settings))
    current_fingerprint = operational.config_fingerprint or computed_fingerprint

    recommendation = repository.latest_recommendation()
    pending_record = repository.latest_recommendation(pending=True)
    es_contract = repository.latest_contract("ES")
    mes_contract = repository.latest_contract("MES")
    es_quote = repository.latest_futures_quote("ES")
    mes_quote = repository.latest_futures_quote("MES")
    es_quote_check = _quote_check(
        es_quote,
        es_contract,
        now=selected_now,
        max_age=quote_max_age,
    )
    mes_quote_check = _quote_check(
        mes_quote,
        mes_contract,
        now=selected_now,
        max_age=quote_max_age,
    )
    inferred_connection = (
        "LIVE" if es_quote_check[0] == "LIVE" and mes_quote_check[0] == "LIVE" else "UNAVAILABLE"
    )
    connection_state = (operational.ibkr_connection or inferred_connection).upper()
    state, reason = _live_decision(
        recommendation,
        current_fingerprint=current_fingerprint,
        connection_state=connection_state,
        now=selected_now,
        quote_max_age=quote_max_age,
        es_contract=es_contract,
        mes_contract=mes_contract,
        es_quote=es_quote,
        mes_quote=mes_quote,
        es_quote_check=es_quote_check,
        mes_quote_check=mes_quote_check,
    )
    history_records = repository.recommendation_history(500)
    historical_record = next(
        (
            item
            for item in history_records
            if _allocation(item) is not None and _required_recommendation_values(item)
        ),
        None,
    )
    display_record = recommendation if state == "LIVE" else historical_record
    recommendation_view = _recommendation_view(
        display_record,
        state=state,
        reason=reason,
        settings=settings,
    )

    pending: PendingView | None = None
    pending_state, _ = _live_decision(
        pending_record,
        current_fingerprint=current_fingerprint,
        connection_state=connection_state,
        now=selected_now,
        quote_max_age=quote_max_age,
        es_contract=es_contract,
        mes_contract=mes_contract,
        es_quote=es_quote,
        mes_quote=mes_quote,
        es_quote_check=es_quote_check,
        mes_quote_check=mes_quote_check,
    )
    if (
        state == "LIVE"
        and pending_state == "LIVE"
        and recommendation is not None
        and pending_record is not None
        and pending_record.target_effective_at is not None
        and pending_record.target_effective_at > selected_now
        and pending_record.target_exposure is not None
        and pending_record.target_exposure != recommendation.target_exposure
    ):
        pending_allocation = _allocation(pending_record)
        if pending_allocation is not None:
            pending = PendingView(
                headline=f"HOLD NEXT: {pending_allocation}",
                effective_at=_display_time(pending_record.target_effective_at),
                target_exposure=_multiple(pending_record.target_exposure),
            )

    snapshots = repository.strategy_snapshots(current_fingerprint, 253)
    latest_snapshot = snapshots[-1] if snapshots else None
    strategy_chart = _strategy_chart_view(repository, current_fingerprint, settings)
    calculation = (
        MetricRow(
            "SPY adjusted close",
            _price(latest_snapshot.spy_close if latest_snapshot else None),
        ),
        MetricRow(
            f"SMA{settings.fast_sma_sessions}",
            _price(latest_snapshot.fast_sma if latest_snapshot else None),
        ),
        MetricRow(
            f"SMA{settings.slow_sma_sessions}",
            _price(latest_snapshot.slow_sma if latest_snapshot else None),
        ),
        MetricRow(
            "Market regime",
            latest_snapshot.regime_state.replace("_", " ").title()
            if latest_snapshot
            else "—",
        ),
        MetricRow(
            "Bearish exit",
            "Armed" if latest_snapshot and latest_snapshot.exit_armed else "Not armed",
        ),
        MetricRow(
            "Post-exit trough",
            _price(latest_snapshot.post_exit_low if latest_snapshot else None),
        ),
        MetricRow(
            "Re-entry level",
            _price(latest_snapshot.reentry_level if latest_snapshot else None),
        ),
        MetricRow(
            f"RV{settings.rv_window_sessions}",
            _percentage(latest_snapshot.rv if latest_snapshot else None),
        ),
        MetricRow(
            "Volatility state",
            latest_snapshot.volatility_state.title() if latest_snapshot else "—",
        ),
        MetricRow(
            "Signal target",
            _multiple(latest_snapshot.target_exposure if latest_snapshot else None),
        ),
        MetricRow(
            "Target effective",
            _display_time(latest_snapshot.effective_at if latest_snapshot else None),
        ),
        MetricRow("Configuration", current_fingerprint[:12]),
    )

    contract_problem = _contract_problem(es_contract, mes_contract, now=selected_now)
    contract_state = "LIVE" if contract_problem is None else "UNAVAILABLE"
    statuses = (
        StatusCell(
            "IBKR connection",
            connection_state.replace("_", " ").title(),
            "LIVE" if connection_state == "LIVE" else "UNAVAILABLE",
        ),
        StatusCell(
            "SPY signal date",
            latest_snapshot.session_date.isoformat() if latest_snapshot else "No snapshot",
            "LIVE" if latest_snapshot else "UNAVAILABLE",
        ),
        StatusCell("ES quote freshness", es_quote_check[1], es_quote_check[0]),
        StatusCell("MES quote freshness", mes_quote_check[1], mes_quote_check[0]),
        StatusCell(
            "ES contract",
            es_contract.local_symbol if es_contract else "Unresolved",
            contract_state,
        ),
        StatusCell(
            "MES contract",
            mes_contract.local_symbol if mes_contract else "Unresolved",
            contract_state,
        ),
        _schedule_status("Next regime evaluation", operational.next_regime_evaluation),
        _schedule_status("Next volatility review", operational.next_volatility_review),
        _schedule_status("Next market open", operational.next_market_open),
    )

    history = tuple(
        HistoryRow(
            calculated_at=_display_time(item.calculated_at),
            allocation=_allocation(item) or "—",
            target=_multiple(item.target_exposure),
            achieved=_multiple(item.achieved_exposure),
            status=item.status,
        )
        for item in history_records[:10]
    )
    return DashboardView(
        recommendation=recommendation_view,
        pending=pending,
        strategy_chart=strategy_chart,
        calculation=calculation,
        statuses=statuses,
        settings=_settings_view(settings),
        history=history,
        csrf_token=csrf_token,
    )
