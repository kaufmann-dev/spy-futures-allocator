from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.errors import ScheduleUnavailable
from app.domain.schedule import (
    RawTargetSnapshot,
    XnysCalendar,
    next_valid_execution,
    next_valid_window,
    reconstruct_schedule,
    signal_window_for,
)

NY = ZoneInfo("America/New_York")


class FakeNyseCalendar:
    def __init__(self, holidays: set[date] | None = None) -> None:
        self.holidays = holidays or set()

    def is_session(self, session_date: date) -> bool:
        return session_date.weekday() < 5 and session_date not in self.holidays

    def session_open(self, session_date: date) -> datetime:
        return datetime.combine(session_date, time(9, 30), NY)

    def session_close(self, session_date: date) -> datetime:
        return datetime.combine(session_date, time(16), NY)


def snapshot(day: date, target: str) -> RawTargetSnapshot:
    return RawTargetSnapshot(
        session_date=day,
        raw_target=Decimal(target),
        calculated_at=datetime.combine(day, time(16, 1), NY),
    )


@pytest.mark.parametrize(
    ("signal_day", "execution_day"),
    [(date(2024, 4, 8), date(2024, 4, 9)), (date(2024, 4, 11), date(2024, 4, 12))],
)
def test_monday_and_thursday_slots_use_the_next_exact_open(
    signal_day: date, execution_day: date
) -> None:
    window = signal_window_for(signal_day, FakeNyseCalendar())
    assert window is not None
    assert window.signal_at == datetime.combine(signal_day, time(16), NY)
    assert window.effective_at == datetime.combine(execution_day, time(9, 30), NY)


def test_holiday_signal_slot_is_skipped_without_carrying_to_tuesday() -> None:
    holiday_monday = date(2024, 1, 15)
    calendar = FakeNyseCalendar({holiday_monday})
    assert signal_window_for(holiday_monday, calendar) is None
    assert signal_window_for(holiday_monday + timedelta(days=1), calendar) is None


def test_slot_is_skipped_when_the_exact_execution_day_is_a_holiday() -> None:
    good_friday = date(2024, 3, 29)
    thursday = date(2024, 3, 28)
    calendar = FakeNyseCalendar({good_friday})
    assert signal_window_for(thursday, calendar) is None


def test_official_xnys_calendar_skips_good_friday_execution_slot() -> None:
    calendar = XnysCalendar()
    assert calendar.is_session(date(2024, 3, 28)) is True
    assert calendar.is_session(date(2024, 3, 29)) is False
    assert signal_window_for(date(2024, 3, 28), calendar) is None


def test_next_signal_before_monday_close_is_that_monday() -> None:
    calendar = FakeNyseCalendar()
    after = datetime(2024, 4, 8, 12, tzinfo=NY)
    window = next_valid_window(after, calendar)
    assert window.signal_session == date(2024, 4, 8)
    assert window.execution_session == date(2024, 4, 9)


def test_next_execution_after_monday_close_is_tuesday_open() -> None:
    calendar = FakeNyseCalendar()
    after = datetime(2024, 4, 8, 16, 1, tzinfo=NY)
    assert next_valid_execution(after, calendar) == datetime(2024, 4, 9, 9, 30, tzinfo=NY)


def test_restart_reconstructs_current_and_pending_target() -> None:
    calendar = FakeNyseCalendar()
    snapshots = [
        snapshot(date(2024, 4, 4), "1.25"),
        snapshot(date(2024, 4, 5), "1.50"),
        snapshot(date(2024, 4, 8), "2.00"),
    ]
    state = reconstruct_schedule(
        snapshots,
        as_of=datetime(2024, 4, 9, 8, tzinfo=NY),
        calendar=calendar,
    )

    assert state.shadow_raw_target == Decimal("2.00")
    assert state.current_scheduled_target == Decimal("1.25")
    assert state.current_target_effective_at == datetime(2024, 4, 5, 9, 30, tzinfo=NY)
    assert state.pending_scheduled_target == Decimal("2.00")
    assert state.pending_target_effective_at == datetime(2024, 4, 9, 9, 30, tzinfo=NY)
    assert state.next_valid_execution_window == datetime(2024, 4, 9, 9, 30, tzinfo=NY)
    assert state.next_valid_signal == datetime(2024, 4, 11, 16, tzinfo=NY)


def test_pending_target_becomes_current_at_execution_open() -> None:
    calendar = FakeNyseCalendar()
    state = reconstruct_schedule(
        [
            snapshot(date(2024, 4, 4), "1.25"),
            snapshot(date(2024, 4, 8), "2.00"),
        ],
        as_of=datetime(2024, 4, 9, 9, 30, tzinfo=NY),
        calendar=calendar,
    )
    assert state.current_scheduled_target == Decimal("2.00")
    assert state.pending_scheduled_target is None


def test_missed_holiday_slot_does_not_change_current_target() -> None:
    good_friday = date(2024, 3, 29)
    calendar = FakeNyseCalendar({good_friday})
    state = reconstruct_schedule(
        [
            snapshot(date(2024, 3, 25), "1.25"),
            snapshot(date(2024, 3, 28), "4.00"),
        ],
        as_of=datetime(2024, 4, 1, 12, tzinfo=NY),
        calendar=calendar,
    )
    assert state.shadow_raw_target == Decimal("4.00")
    assert state.current_scheduled_target == Decimal("1.25")
    assert state.pending_scheduled_target is None


def test_reconstruction_fails_closed_without_an_effective_target() -> None:
    calendar = FakeNyseCalendar()
    with pytest.raises(ScheduleUnavailable, match="cannot be reconstructed"):
        reconstruct_schedule(
            [snapshot(date(2024, 4, 8), "2.00")],
            as_of=datetime(2024, 4, 9, 8, tzinfo=NY),
            calendar=calendar,
        )


def test_reconstruction_rejects_pre_close_calculation() -> None:
    calendar = FakeNyseCalendar()
    invalid = RawTargetSnapshot(
        session_date=date(2024, 4, 8),
        raw_target=Decimal("2"),
        calculated_at=datetime(2024, 4, 8, 15, 59, tzinfo=NY),
    )
    with pytest.raises(ScheduleUnavailable, match="before its session closed"):
        reconstruct_schedule(
            [invalid],
            as_of=datetime(2024, 4, 9, 10, tzinfo=NY),
            calendar=calendar,
        )
