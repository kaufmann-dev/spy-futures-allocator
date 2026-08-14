"""Generic next-XNYS-session-open scheduling for strategy target changes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from .errors import DomainError, ScheduleUnavailable
from .types import StrategyConfig, decimal_value

ZERO = Decimal("0")
NEW_YORK = ZoneInfo("America/New_York")


class MarketCalendar(Protocol):
    """The calendar surface required by deterministic scheduling."""

    def is_session(self, session_date: date) -> bool: ...

    def session_open(self, session_date: date) -> datetime: ...

    def session_close(self, session_date: date) -> datetime: ...


class XnysCalendar:
    """Adapter for exchange-calendars' official NYSE session calendar."""

    def __init__(self) -> None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise ScheduleUnavailable("exchange-calendars is not installed") from exc
        self._calendar = xcals.get_calendar("XNYS")

    def is_session(self, session_date: date) -> bool:
        return bool(self._calendar.is_session(session_date.isoformat()))

    def session_open(self, session_date: date) -> datetime:
        return _calendar_datetime(self._calendar.session_open(session_date.isoformat()))

    def session_close(self, session_date: date) -> datetime:
        return _calendar_datetime(self._calendar.session_close(session_date.isoformat()))


def _calendar_datetime(value: object) -> datetime:
    converted = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if not isinstance(converted, datetime) or converted.tzinfo is None:
        raise ScheduleUnavailable("market calendar returned a naive timestamp")
    return converted


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleUnavailable(f"{field} must include a timezone")


def _require_date(value: object, *, field: str) -> date:
    if not isinstance(value, date) or type(value) is not date:
        raise ScheduleUnavailable(f"{field} must be a date")
    return value


@dataclass(frozen=True, slots=True)
class TargetSignal:
    """A changed signal target calculated after a completed session close."""

    session_date: date
    target_exposure: Decimal
    calculated_at: datetime

    def __post_init__(self) -> None:
        _require_date(self.session_date, field="session_date")
        try:
            target = decimal_value(self.target_exposure, field="target_exposure")
        except DomainError as exc:
            raise ScheduleUnavailable(str(exc)) from exc
        if target < ZERO:
            raise ScheduleUnavailable("target_exposure must be nonnegative")
        _require_aware(self.calculated_at, field="calculated_at")
        object.__setattr__(self, "target_exposure", target)


@dataclass(frozen=True, slots=True)
class ScheduledTargetInstruction:
    """A target signal and the next real session open where it takes effect."""

    signal_session: date
    signal_at: datetime
    target_exposure: Decimal
    execution_session: date
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduleState:
    """Current and pending target reconstructed solely from target signals."""

    latest_signal_target: Decimal
    latest_signal_session: date
    current_target: Decimal
    current_target_effective_at: datetime
    pending_target: Decimal | None
    pending_target_effective_at: datetime | None
    next_market_open: datetime


def _next_session_date(after_session: date, calendar: MarketCalendar) -> date:
    for offset in range(1, 371):
        candidate = after_session + timedelta(days=offset)
        if calendar.is_session(candidate):
            return candidate
    raise ScheduleUnavailable("no XNYS session found within one year")


def schedule_target(
    signal: TargetSignal, calendar: MarketCalendar
) -> ScheduledTargetInstruction:
    """Schedule one close-derived target for the next actual XNYS open."""

    if not isinstance(signal, TargetSignal):
        raise ScheduleUnavailable("target history contains an invalid signal")
    if not calendar.is_session(signal.session_date):
        raise ScheduleUnavailable("target signal is not an XNYS session")
    signal_at = calendar.session_close(signal.session_date)
    _require_aware(signal_at, field="signal_at")
    if signal.calculated_at < signal_at:
        raise ScheduleUnavailable("target was calculated before its session closed")

    execution_session = _next_session_date(signal.session_date, calendar)
    effective_at = calendar.session_open(execution_session)
    _require_aware(effective_at, field="effective_at")
    if effective_at <= signal_at:
        raise ScheduleUnavailable("execution open must follow the signal close")
    return ScheduledTargetInstruction(
        signal_session=signal.session_date,
        signal_at=signal_at,
        target_exposure=signal.target_exposure,
        execution_session=execution_session,
        effective_at=effective_at,
    )


def next_session_open(after: datetime, calendar: MarketCalendar) -> datetime:
    """Return the first actual XNYS session open strictly after ``after``."""

    _require_aware(after, field="after")
    first_date = after.astimezone(NEW_YORK).date()
    for offset in range(371):
        candidate = first_date + timedelta(days=offset)
        if not calendar.is_session(candidate):
            continue
        market_open = calendar.session_open(candidate)
        _require_aware(market_open, field="market_open")
        if market_open > after:
            return market_open
    raise ScheduleUnavailable("no future XNYS open found within one year")


def is_review_session(
    session_date: date, config: StrategyConfig, calendar: MarketCalendar
) -> bool:
    """Return true only for a configured weekday that is itself an XNYS session."""

    _require_date(session_date, field="session_date")
    if not isinstance(config, StrategyConfig):
        raise ScheduleUnavailable("config must be a StrategyConfig")
    return config.reviews_on(session_date) and calendar.is_session(session_date)


def next_review_close(
    after: datetime, config: StrategyConfig, calendar: MarketCalendar
) -> datetime:
    """Return the next configured review close without carrying holidays forward."""

    _require_aware(after, field="after")
    first_date = after.astimezone(NEW_YORK).date()
    for offset in range(371):
        candidate = first_date + timedelta(days=offset)
        if not is_review_session(candidate, config, calendar):
            continue
        market_close = calendar.session_close(candidate)
        _require_aware(market_close, field="market_close")
        if market_close > after:
            return market_close
    raise ScheduleUnavailable("no configured review close found within one year")


def reconstruct_schedule(
    signals: Iterable[TargetSignal],
    *,
    as_of: datetime,
    calendar: MarketCalendar,
) -> ScheduleState:
    """Reconstruct current and pending targets from stored changed-target signals."""

    _require_aware(as_of, field="as_of")
    ordered = list(signals)
    if not ordered:
        raise ScheduleUnavailable("no target signals are available")

    previous_date: date | None = None
    visible: list[tuple[TargetSignal, ScheduledTargetInstruction]] = []
    for signal in ordered:
        if not isinstance(signal, TargetSignal):
            raise ScheduleUnavailable("target history contains an invalid signal")
        if previous_date is not None and signal.session_date <= previous_date:
            raise ScheduleUnavailable("target signals must have unique, increasing session dates")
        previous_date = signal.session_date
        instruction = schedule_target(signal, calendar)
        if signal.calculated_at <= as_of:
            visible.append((signal, instruction))

    if not visible:
        raise ScheduleUnavailable("no completed target signal exists at this time")

    current: ScheduledTargetInstruction | None = None
    pending: ScheduledTargetInstruction | None = None
    for _, instruction in visible:
        if instruction.effective_at <= as_of:
            current = instruction
        elif pending is None or instruction.effective_at < pending.effective_at:
            pending = instruction

    if current is None:
        raise ScheduleUnavailable("current target cannot be reconstructed before its first open")

    latest_signal = visible[-1][0]
    return ScheduleState(
        latest_signal_target=latest_signal.target_exposure,
        latest_signal_session=latest_signal.session_date,
        current_target=current.target_exposure,
        current_target_effective_at=current.effective_at,
        pending_target=pending.target_exposure if pending is not None else None,
        pending_target_effective_at=pending.effective_at if pending is not None else None,
        next_market_open=next_session_open(as_of, calendar),
    )
