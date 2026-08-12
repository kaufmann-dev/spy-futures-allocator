"""Monday/Thursday signal scheduling against the official XNYS calendar."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from .calculation import MAX_TARGET, ZERO
from .errors import DomainError, ScheduleUnavailable
from .types import decimal_value

NEW_YORK = ZoneInfo("America/New_York")


class MarketCalendar(Protocol):
    """The small calendar surface used by the deterministic scheduler."""

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
        value = self._calendar.session_open(session_date.isoformat())
        return _calendar_datetime(value)

    def session_close(self, session_date: date) -> datetime:
        value = self._calendar.session_close(session_date.isoformat())
        return _calendar_datetime(value)


def _calendar_datetime(value: object) -> datetime:
    converted = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if not isinstance(converted, datetime) or converted.tzinfo is None:
        raise ScheduleUnavailable("market calendar returned a naive timestamp")
    return converted


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleUnavailable(f"{field} must include a timezone")


@dataclass(frozen=True, slots=True)
class SignalWindow:
    signal_session: date
    signal_at: datetime
    execution_session: date
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class RawTargetSnapshot:
    session_date: date
    raw_target: Decimal
    calculated_at: datetime

    def __post_init__(self) -> None:
        try:
            target = decimal_value(self.raw_target, field="raw_target")
        except DomainError as exc:
            raise ScheduleUnavailable(str(exc)) from exc
        if target < ZERO or target > MAX_TARGET:
            raise ScheduleUnavailable("raw_target must be between 0 and 4.85")
        _require_aware(self.calculated_at, field="calculated_at")
        object.__setattr__(self, "raw_target", target)


@dataclass(frozen=True, slots=True)
class ScheduleState:
    shadow_raw_target: Decimal
    current_scheduled_target: Decimal
    current_target_effective_at: datetime
    pending_scheduled_target: Decimal | None
    pending_target_effective_at: datetime | None
    next_valid_signal: datetime
    next_valid_execution_window: datetime


def signal_window_for(signal_session: date, calendar: MarketCalendar) -> SignalWindow | None:
    """Return a slot only when both exact calendar days are normal sessions."""

    weekday = signal_session.weekday()
    if weekday not in (0, 3) or not calendar.is_session(signal_session):
        return None
    execution_session = signal_session + timedelta(days=1)
    required_execution_weekday = 1 if weekday == 0 else 4
    if execution_session.weekday() != required_execution_weekday or not calendar.is_session(
        execution_session
    ):
        return None

    signal_at = calendar.session_close(signal_session)
    effective_at = calendar.session_open(execution_session)
    _require_aware(signal_at, field="signal_at")
    _require_aware(effective_at, field="effective_at")
    if effective_at <= signal_at:
        raise ScheduleUnavailable("execution window must follow the signal close")
    return SignalWindow(
        signal_session=signal_session,
        signal_at=signal_at,
        execution_session=execution_session,
        effective_at=effective_at,
    )


def is_valid_signal_session(session_date: date, calendar: MarketCalendar) -> bool:
    return signal_window_for(session_date, calendar) is not None


def next_valid_window(after: datetime, calendar: MarketCalendar) -> SignalWindow:
    """Find the next signal close strictly after a timestamp."""

    _require_aware(after, field="after")
    candidate = after.astimezone(NEW_YORK).date()
    for offset in range(371):
        window = signal_window_for(candidate + timedelta(days=offset), calendar)
        if window is not None and window.signal_at > after:
            return window
    raise ScheduleUnavailable("no valid signal window found within one year")


def next_valid_execution(after: datetime, calendar: MarketCalendar) -> datetime:
    """Find the next Tuesday/Friday open strictly after a timestamp.

    Unlike ``next_valid_window``, this can return tomorrow's execution after its
    Monday/Thursday signal has already closed. This is used for event recovery.
    """

    _require_aware(after, field="after")
    candidate = after.astimezone(NEW_YORK).date() - timedelta(days=1)
    for offset in range(372):
        window = signal_window_for(candidate + timedelta(days=offset), calendar)
        if window is not None and window.effective_at > after:
            return window.effective_at
    raise ScheduleUnavailable("no valid execution window found within one year")


def reconstruct_schedule(
    snapshots: Iterable[RawTargetSnapshot],
    *,
    as_of: datetime,
    calendar: MarketCalendar,
) -> ScheduleState:
    """Rebuild current and pending targets solely from stored close snapshots."""

    _require_aware(as_of, field="as_of")
    ordered = list(snapshots)
    if not ordered:
        raise ScheduleUnavailable("no target snapshots are available")

    previous_date: date | None = None
    visible: list[RawTargetSnapshot] = []
    for snapshot in ordered:
        if not isinstance(snapshot, RawTargetSnapshot):
            raise ScheduleUnavailable("target history contains an invalid snapshot")
        if previous_date is not None and snapshot.session_date <= previous_date:
            raise ScheduleUnavailable("target snapshots must have unique, increasing session dates")
        previous_date = snapshot.session_date
        if not calendar.is_session(snapshot.session_date):
            raise ScheduleUnavailable("target snapshot is not an XNYS session")
        session_close = calendar.session_close(snapshot.session_date)
        _require_aware(session_close, field="session_close")
        if snapshot.calculated_at < session_close:
            raise ScheduleUnavailable("target was calculated before its session closed")
        if snapshot.calculated_at <= as_of:
            visible.append(snapshot)

    if not visible:
        raise ScheduleUnavailable("no completed target snapshot exists at this time")

    current: tuple[Decimal, datetime] | None = None
    pending: tuple[Decimal, datetime] | None = None
    for snapshot in visible:
        window = signal_window_for(snapshot.session_date, calendar)
        if window is None:
            continue
        candidate = (snapshot.raw_target, window.effective_at)
        if window.effective_at <= as_of:
            current = candidate
        elif pending is None or window.effective_at < pending[1]:
            pending = candidate

    if current is None:
        raise ScheduleUnavailable("current scheduled target cannot be reconstructed")

    future = next_valid_window(as_of, calendar)
    next_execution = pending[1] if pending is not None else next_valid_execution(as_of, calendar)
    return ScheduleState(
        shadow_raw_target=visible[-1].raw_target,
        current_scheduled_target=current[0],
        current_target_effective_at=current[1],
        pending_scheduled_target=pending[0] if pending else None,
        pending_target_effective_at=pending[1] if pending else None,
        next_valid_signal=future.signal_at,
        next_valid_execution_window=next_execution,
    )
