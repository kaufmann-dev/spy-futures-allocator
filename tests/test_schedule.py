from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.errors import ScheduleUnavailable
from app.domain.schedule import (
    TargetSignal,
    XnysCalendar,
    is_review_session,
    next_review_close,
    next_session_open,
    reconstruct_schedule,
    schedule_target,
)
from app.domain.types import StrategyConfig

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


def signal(day: date, target: str) -> TargetSignal:
    return TargetSignal(
        session_date=day,
        target_exposure=Decimal(target),
        calculated_at=datetime.combine(day, time(16, 1), NY),
    )


@pytest.mark.parametrize(
    ("signal_day", "execution_day"),
    [
        (date(2024, 4, 8), date(2024, 4, 9)),
        (date(2024, 4, 11), date(2024, 4, 12)),
        (date(2024, 4, 12), date(2024, 4, 15)),
    ],
)
def test_every_target_change_uses_the_next_actual_session_open(
    signal_day: date, execution_day: date
) -> None:
    instruction = schedule_target(signal(signal_day, "1.6"), FakeNyseCalendar())

    assert instruction.signal_session == signal_day
    assert instruction.signal_at == datetime.combine(signal_day, time(16), NY)
    assert instruction.execution_session == execution_day
    assert instruction.effective_at == datetime.combine(execution_day, time(9, 30), NY)


def test_execution_advances_past_a_holiday_instead_of_discarding_the_signal() -> None:
    good_friday = date(2024, 3, 29)
    thursday = date(2024, 3, 28)

    instruction = schedule_target(
        signal(thursday, "0"), FakeNyseCalendar({good_friday})
    )

    assert instruction.execution_session == date(2024, 4, 1)


def test_official_xnys_calendar_uses_monday_after_good_friday() -> None:
    instruction = schedule_target(signal(date(2024, 3, 28), "0"), XnysCalendar())

    assert instruction.execution_session == date(2024, 4, 1)


def test_configured_weekday_holiday_is_skipped_without_a_carry_review() -> None:
    holiday_monday = date(2024, 1, 15)
    calendar = FakeNyseCalendar({holiday_monday})
    selected = StrategyConfig()

    assert is_review_session(holiday_monday, selected, calendar) is False
    assert is_review_session(holiday_monday + timedelta(days=1), selected, calendar) is False
    assert next_review_close(
        datetime(2024, 1, 12, 17, tzinfo=NY), selected, calendar
    ) == datetime(2024, 1, 18, 16, tzinfo=NY)


def test_custom_review_weekdays_are_honored() -> None:
    selected = StrategyConfig(review_weekdays=(1, 2))
    calendar = FakeNyseCalendar()

    assert is_review_session(date(2024, 4, 9), selected, calendar) is True
    assert is_review_session(date(2024, 4, 10), selected, calendar) is True
    assert is_review_session(date(2024, 4, 11), selected, calendar) is False


def test_next_session_open_can_be_today_or_a_later_session() -> None:
    calendar = FakeNyseCalendar()

    assert next_session_open(
        datetime(2024, 4, 8, 8, tzinfo=NY), calendar
    ) == datetime(2024, 4, 8, 9, 30, tzinfo=NY)
    assert next_session_open(
        datetime(2024, 4, 12, 10, tzinfo=NY), calendar
    ) == datetime(2024, 4, 15, 9, 30, tzinfo=NY)


def test_restart_reconstructs_current_and_pending_target() -> None:
    calendar = FakeNyseCalendar()
    state = reconstruct_schedule(
        [signal(date(2024, 4, 4), "1.60"), signal(date(2024, 4, 8), "0.60")],
        as_of=datetime(2024, 4, 9, 8, tzinfo=NY),
        calendar=calendar,
    )

    assert state.latest_signal_target == Decimal("0.60")
    assert state.latest_signal_session == date(2024, 4, 8)
    assert state.current_target == Decimal("1.60")
    assert state.current_target_effective_at == datetime(2024, 4, 5, 9, 30, tzinfo=NY)
    assert state.pending_target == Decimal("0.60")
    assert state.pending_target_effective_at == datetime(2024, 4, 9, 9, 30, tzinfo=NY)
    assert state.next_market_open == datetime(2024, 4, 9, 9, 30, tzinfo=NY)


def test_pending_target_becomes_current_at_its_execution_open() -> None:
    state = reconstruct_schedule(
        [signal(date(2024, 4, 4), "1.60"), signal(date(2024, 4, 8), "0.60")],
        as_of=datetime(2024, 4, 9, 9, 30, tzinfo=NY),
        calendar=FakeNyseCalendar(),
    )

    assert state.current_target == Decimal("0.60")
    assert state.pending_target is None


def test_scheduling_allows_arbitrary_nonnegative_exposure() -> None:
    instruction = schedule_target(signal(date(2024, 4, 8), "8.25"), FakeNyseCalendar())

    assert instruction.target_exposure == Decimal("8.25")


def test_reconstruction_fails_closed_before_the_first_effective_open() -> None:
    with pytest.raises(ScheduleUnavailable, match="before its first open"):
        reconstruct_schedule(
            [signal(date(2024, 4, 8), "1.60")],
            as_of=datetime(2024, 4, 9, 8, tzinfo=NY),
            calendar=FakeNyseCalendar(),
        )


def test_reconstruction_rejects_pre_close_calculation_and_duplicate_sessions() -> None:
    invalid = TargetSignal(
        session_date=date(2024, 4, 8),
        target_exposure=Decimal("1.6"),
        calculated_at=datetime(2024, 4, 8, 15, 59, tzinfo=NY),
    )
    with pytest.raises(ScheduleUnavailable, match="before its session closed"):
        reconstruct_schedule(
            [invalid],
            as_of=datetime(2024, 4, 9, 10, tzinfo=NY),
            calendar=FakeNyseCalendar(),
        )

    duplicate = signal(date(2024, 4, 8), "1.6")
    with pytest.raises(ScheduleUnavailable, match="unique, increasing"):
        reconstruct_schedule(
            [duplicate, duplicate],
            as_of=datetime(2024, 4, 10, 10, tzinfo=NY),
            calendar=FakeNyseCalendar(),
        )


def test_signal_rejects_negative_target_and_naive_timestamp() -> None:
    with pytest.raises(ScheduleUnavailable, match="nonnegative"):
        signal(date(2024, 4, 8), "-0.01")
    with pytest.raises(ScheduleUnavailable, match="timezone"):
        TargetSignal(date(2024, 4, 8), Decimal("1.6"), datetime(2024, 4, 8, 16, 1))
