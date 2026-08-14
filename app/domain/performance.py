"""Historical SPY-return proxy replay with explicit exposure timing.

This module intentionally models a SPY simple-return proxy, not ES/MES performance.
It excludes futures basis, contract rolls, financing, commissions, and slippage.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from .calculation import StrategyReplay
from .errors import DomainError, PerformanceUnavailable
from .types import DecimalLike, decimal_value

ZERO = Decimal("0")
ONE = Decimal("1")
NORMALIZED_START = Decimal("100")


def _performance_decimal(value: DecimalLike, *, field: str) -> Decimal:
    try:
        return decimal_value(value, field=field)
    except DomainError as exc:
        raise PerformanceUnavailable(str(exc)) from exc


def _require_session_date(value: object, *, field: str) -> None:
    if not isinstance(value, date) or type(value) is not date:
        raise PerformanceUnavailable(f"{field} must be a date")


@dataclass(frozen=True, slots=True)
class SpyOhlcBar:
    """One regular-session SPY OHLC bar."""

    session_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    completed: bool = True

    def __post_init__(self) -> None:
        _require_session_date(self.session_date, field="session_date")
        if not isinstance(self.completed, bool):
            raise PerformanceUnavailable("completed must be boolean")

        open_price = _performance_decimal(self.open_price, field="open_price")
        high_price = _performance_decimal(self.high_price, field="high_price")
        low_price = _performance_decimal(self.low_price, field="low_price")
        close_price = _performance_decimal(self.close_price, field="close_price")
        if min(open_price, high_price, low_price, close_price) <= ZERO:
            raise PerformanceUnavailable("OHLC prices must be positive")
        if high_price < max(open_price, close_price):
            raise PerformanceUnavailable("high_price must contain the open and close")
        if low_price > min(open_price, close_price):
            raise PerformanceUnavailable("low_price must contain the open and close")

        object.__setattr__(self, "open_price", open_price)
        object.__setattr__(self, "high_price", high_price)
        object.__setattr__(self, "low_price", low_price)
        object.__setattr__(self, "close_price", close_price)


@dataclass(frozen=True, slots=True)
class ExposureSchedulePoint:
    """Long-only exposure effective at this session's open."""

    session_date: date
    open_exposure: Decimal

    def __post_init__(self) -> None:
        _require_session_date(self.session_date, field="session_date")
        open_exposure = _performance_decimal(self.open_exposure, field="open_exposure")
        if open_exposure < ZERO:
            raise PerformanceUnavailable("open_exposure must be nonnegative")

        object.__setattr__(self, "open_exposure", open_exposure)

    @property
    def close_exposure(self) -> Decimal:
        """Exposure carried from this close until the following session opens."""

        return self.open_exposure


@dataclass(frozen=True, slots=True)
class PerformancePoint:
    """One immutable chart point, measured at the completed session close."""

    session_date: date
    spy_value: Decimal
    strategy_value: Decimal
    spy_daily_return: Decimal | None
    strategy_daily_return: Decimal | None
    spy_drawdown: Decimal
    strategy_drawdown: Decimal
    overnight_exposure: Decimal | None
    intraday_exposure: Decimal
    close_exposure: Decimal
    strategy_at_floor: bool


@dataclass(frozen=True, slots=True)
class PerformanceSeries:
    """Complete normalized chart series and close-to-close maximum drawdowns."""

    points: tuple[PerformancePoint, ...]
    spy_max_drawdown: Decimal
    strategy_max_drawdown: Decimal


def exposure_schedule_from_replay(
    replay: StrategyReplay, *, initial_exposure: DecimalLike = ZERO
) -> tuple[ExposureSchedulePoint, ...]:
    """Shift close-derived replay targets to the following observed session open.

    The replay's first calculable close cannot affect that same session, so the
    caller-supplied initial exposure applies there. Every later session receives
    the target selected at the immediately preceding completed close. Because
    replay bars contain actual sessions, exchange holidays require no carry rule.
    """

    if not isinstance(replay, StrategyReplay):
        raise PerformanceUnavailable("replay must be a StrategyReplay")
    initial = _performance_decimal(initial_exposure, field="initial_exposure")
    if initial < ZERO:
        raise PerformanceUnavailable("initial_exposure must be nonnegative")
    if not replay.snapshots:
        raise PerformanceUnavailable("strategy replay is empty")

    points = [
        ExposureSchedulePoint(
            session_date=replay.snapshots[0].session_date,
            open_exposure=initial,
        )
    ]
    points.extend(
        ExposureSchedulePoint(
            session_date=current.session_date,
            open_exposure=previous.target_exposure,
        )
        for previous, current in zip(replay.snapshots, replay.snapshots[1:], strict=False)
    )
    return tuple(points)


def _validate_ordered_bars(bars: Sequence[object]) -> list[SpyOhlcBar]:
    if not bars:
        raise PerformanceUnavailable("completed SPY history is missing")

    validated: list[SpyOhlcBar] = []
    previous_date: date | None = None
    for candidate in bars:
        if not isinstance(candidate, SpyOhlcBar):
            raise PerformanceUnavailable("SPY history contains an invalid OHLC bar")
        if not candidate.completed:
            raise PerformanceUnavailable(
                f"SPY bar for {candidate.session_date.isoformat()} is not completed"
            )
        if previous_date is not None and candidate.session_date <= previous_date:
            raise PerformanceUnavailable(
                "SPY bars must have unique, strictly increasing session dates"
            )
        validated.append(candidate)
        previous_date = candidate.session_date
    return validated


def _validate_ordered_schedule(
    schedule: Sequence[object],
) -> list[ExposureSchedulePoint]:
    if not schedule:
        raise PerformanceUnavailable("exposure schedule is missing")

    validated: list[ExposureSchedulePoint] = []
    previous_date: date | None = None
    for candidate in schedule:
        if not isinstance(candidate, ExposureSchedulePoint):
            raise PerformanceUnavailable("exposure schedule contains an invalid point")
        if previous_date is not None and candidate.session_date <= previous_date:
            raise PerformanceUnavailable(
                "exposure schedule must have unique, strictly increasing session dates"
            )
        validated.append(candidate)
        previous_date = candidate.session_date
    return validated


def _require_aligned_sessions(
    bars: Sequence[SpyOhlcBar], schedule: Sequence[ExposureSchedulePoint]
) -> None:
    if len(bars) != len(schedule):
        raise PerformanceUnavailable(
            "each session requires one completed SPY bar and one exposure schedule point"
        )
    for bar, exposure in zip(bars, schedule, strict=True):
        if bar.session_date != exposure.session_date:
            raise PerformanceUnavailable(
                "SPY and exposure sessions are misaligned at "
                f"{bar.session_date.isoformat()} and {exposure.session_date.isoformat()}"
            )


def _leveraged_factor(price_return: Decimal, exposure: Decimal) -> Decimal:
    """Apply long-only simple leverage without allowing proxy equity below zero."""

    return max(ZERO, ONE + exposure * price_return)


def _drawdown(value: Decimal, peak: Decimal) -> Decimal:
    return value / peak - ONE


def calculate_performance(
    bars: Iterable[SpyOhlcBar],
    schedule: Iterable[ExposureSchedulePoint],
) -> PerformanceSeries:
    """Replay close-valued SPY and strategy growth from a normalized $100.

    The first completed close is the common $100 baseline. For every later
    session, the prior session's close exposure applies from prior close to the
    current open; the current session's open exposure then applies from open to
    close. Each leveraged leg is floored at zero, and a proxy that reaches zero
    remains there. No missing session or exposure is carried forward.
    """

    ordered_bars = _validate_ordered_bars(list(bars))
    ordered_schedule = _validate_ordered_schedule(list(schedule))
    _require_aligned_sessions(ordered_bars, ordered_schedule)

    with localcontext() as context:
        context.prec = 50
        spy_value = NORMALIZED_START
        strategy_value = NORMALIZED_START
        spy_peak = NORMALIZED_START
        strategy_peak = NORMALIZED_START
        spy_max_drawdown = ZERO
        strategy_max_drawdown = ZERO

        first_bar = ordered_bars[0]
        first_schedule = ordered_schedule[0]
        points = [
            PerformancePoint(
                session_date=first_bar.session_date,
                spy_value=spy_value,
                strategy_value=strategy_value,
                spy_daily_return=None,
                strategy_daily_return=None,
                spy_drawdown=ZERO,
                strategy_drawdown=ZERO,
                overnight_exposure=None,
                intraday_exposure=first_schedule.open_exposure,
                close_exposure=first_schedule.close_exposure,
                strategy_at_floor=False,
            )
        ]

        previous_bar = first_bar
        previous_schedule = first_schedule
        for bar, exposure in zip(ordered_bars[1:], ordered_schedule[1:], strict=True):
            spy_daily_return = bar.close_price / previous_bar.close_price - ONE
            spy_value *= ONE + spy_daily_return

            overnight_price_return = bar.open_price / previous_bar.close_price - ONE
            intraday_price_return = bar.close_price / bar.open_price - ONE
            overnight_factor = _leveraged_factor(
                overnight_price_return, previous_schedule.close_exposure
            )
            intraday_factor = _leveraged_factor(intraday_price_return, exposure.open_exposure)
            if strategy_value == ZERO:
                strategy_daily_return = ZERO
            else:
                strategy_daily_return = overnight_factor * intraday_factor - ONE
                strategy_value *= ONE + strategy_daily_return

            spy_peak = max(spy_peak, spy_value)
            strategy_peak = max(strategy_peak, strategy_value)
            spy_drawdown = _drawdown(spy_value, spy_peak)
            strategy_drawdown = _drawdown(strategy_value, strategy_peak)
            spy_max_drawdown = min(spy_max_drawdown, spy_drawdown)
            strategy_max_drawdown = min(strategy_max_drawdown, strategy_drawdown)

            points.append(
                PerformancePoint(
                    session_date=bar.session_date,
                    spy_value=spy_value,
                    strategy_value=strategy_value,
                    spy_daily_return=spy_daily_return,
                    strategy_daily_return=strategy_daily_return,
                    spy_drawdown=spy_drawdown,
                    strategy_drawdown=strategy_drawdown,
                    overnight_exposure=previous_schedule.close_exposure,
                    intraday_exposure=exposure.open_exposure,
                    close_exposure=exposure.close_exposure,
                    strategy_at_floor=strategy_value == ZERO,
                )
            )
            previous_bar = bar
            previous_schedule = exposure

    return PerformanceSeries(
        points=tuple(points),
        spy_max_drawdown=spy_max_drawdown,
        strategy_max_drawdown=strategy_max_drawdown,
    )
