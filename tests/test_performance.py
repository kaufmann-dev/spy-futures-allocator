from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.performance import (
    ExposureSchedulePoint,
    PerformanceUnavailable,
    SpyOhlcBar,
    calculate_performance,
)


def bar(
    offset: int,
    *,
    open_price: str = "100",
    close_price: str = "100",
    high_price: str | None = None,
    low_price: str | None = None,
    completed: bool = True,
) -> SpyOhlcBar:
    open_value = Decimal(open_price)
    close_value = Decimal(close_price)
    return SpyOhlcBar(
        session_date=date(2024, 1, 2) + timedelta(days=offset),
        open_price=open_value,
        high_price=Decimal(high_price) if high_price is not None else max(open_value, close_value),
        low_price=Decimal(low_price) if low_price is not None else min(open_value, close_value),
        close_price=close_value,
        completed=completed,
    )


def exposure(
    offset: int, open_exposure: str, *, close_cap: str | None = None
) -> ExposureSchedulePoint:
    return ExposureSchedulePoint(
        session_date=date(2024, 1, 2) + timedelta(days=offset),
        open_exposure=Decimal(open_exposure),
        close_cap=Decimal(close_cap) if close_cap is not None else None,
    )


def test_constant_price_keeps_both_series_at_one_hundred() -> None:
    result = calculate_performance(
        [bar(0), bar(1), bar(2)],
        [exposure(0, "4.85"), exposure(1, "0"), exposure(2, "2")],
    )

    assert tuple(point.spy_value for point in result.points) == (
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
    )
    assert tuple(point.strategy_value for point in result.points) == (
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
    )
    assert all(point.spy_drawdown == 0 for point in result.points)
    assert all(point.strategy_drawdown == 0 for point in result.points)
    assert result.spy_max_drawdown == 0
    assert result.strategy_max_drawdown == 0


def test_open_change_uses_old_exposure_overnight_and_new_exposure_intraday() -> None:
    result = calculate_performance(
        [bar(0), bar(1, open_price="110", close_price="121")],
        [exposure(0, "1"), exposure(1, "2")],
    )

    point = result.points[1]
    assert point.spy_value == Decimal("121")
    assert point.strategy_value == Decimal("132")
    assert point.strategy_daily_return == Decimal("0.32")
    assert point.overnight_exposure == Decimal("1")
    assert point.intraday_exposure == Decimal("2")


def test_close_cap_only_reduces_the_next_overnight_leg() -> None:
    result = calculate_performance(
        [
            bar(0),
            bar(1, open_price="100", close_price="110"),
            bar(2, open_price="88", close_price="96.8"),
        ],
        [exposure(0, "1"), exposure(1, "2", close_cap="0"), exposure(2, "1")],
    )

    assert result.points[1].strategy_value == Decimal("120")
    assert result.points[1].close_exposure == 0
    assert result.points[2].overnight_exposure == 0
    assert result.points[2].strategy_value == Decimal("132.0")


def test_zero_exposure_avoids_a_spy_drawdown() -> None:
    result = calculate_performance(
        [bar(0), bar(1, open_price="100", close_price="50"), bar(2, open_price="50")],
        [exposure(0, "0"), exposure(1, "0"), exposure(2, "0")],
    )

    assert result.points[1].spy_value == Decimal("50")
    assert result.points[1].spy_drawdown == Decimal("-0.5")
    assert result.spy_max_drawdown == Decimal("-0.5")
    assert all(point.strategy_value == 100 for point in result.points)
    assert result.strategy_max_drawdown == 0


def test_leveraged_gain_and_loss_compound_from_the_previous_proxy_value() -> None:
    result = calculate_performance(
        [
            bar(0),
            bar(1, open_price="100", close_price="110"),
            bar(2, open_price="110", close_price="99"),
        ],
        [exposure(0, "2"), exposure(1, "2"), exposure(2, "2")],
    )

    assert tuple(point.strategy_value for point in result.points) == (
        Decimal("100"),
        Decimal("120"),
        Decimal("96.0"),
    )
    assert result.points[2].strategy_daily_return == Decimal("-0.2")
    assert result.points[2].strategy_drawdown == Decimal("-0.2")
    assert result.strategy_max_drawdown == Decimal("-0.2")


def test_drawdown_uses_the_running_peak_and_retains_the_worst_daily_value() -> None:
    result = calculate_performance(
        [
            bar(0),
            bar(1, open_price="100", close_price="120"),
            bar(2, open_price="120", close_price="90"),
            bar(3, open_price="90", close_price="108"),
        ],
        [exposure(index, "1") for index in range(4)],
    )

    assert tuple(point.spy_drawdown for point in result.points) == (
        Decimal("0"),
        Decimal("0"),
        Decimal("-0.25"),
        Decimal("-0.1"),
    )
    assert result.spy_max_drawdown == Decimal("-0.25")
    assert result.strategy_max_drawdown == Decimal("-0.25")


def test_proxy_is_floored_at_zero_and_cannot_recover_after_a_wipeout() -> None:
    result = calculate_performance(
        [
            bar(0),
            bar(1, open_price="100", close_price="70"),
            bar(2, open_price="70", close_price="140"),
        ],
        [exposure(0, "4"), exposure(1, "4"), exposure(2, "4")],
    )

    wiped_out = result.points[1]
    assert wiped_out.strategy_value == 0
    assert wiped_out.strategy_daily_return == -1
    assert wiped_out.strategy_drawdown == -1
    assert wiped_out.strategy_at_floor is True
    assert result.points[2].strategy_value == 0
    assert result.points[2].strategy_daily_return == 0
    assert result.strategy_max_drawdown == -1


@pytest.mark.parametrize(
    ("open_price", "close_price", "high_price", "low_price", "message"),
    [
        ("0", "100", None, None, "positive"),
        ("100", "110", "109", None, "high_price"),
        ("100", "90", None, "91", "low_price"),
    ],
)
def test_invalid_ohlc_bar_fails_closed(
    open_price: str,
    close_price: str,
    high_price: str | None,
    low_price: str | None,
    message: str,
) -> None:
    with pytest.raises(PerformanceUnavailable, match=message):
        bar(
            0,
            open_price=open_price,
            close_price=close_price,
            high_price=high_price,
            low_price=low_price,
        )


def test_incomplete_bar_fails_closed_instead_of_being_skipped() -> None:
    with pytest.raises(PerformanceUnavailable, match="not completed"):
        calculate_performance(
            [bar(0), bar(1, completed=False)],
            [exposure(0, "1"), exposure(1, "1")],
        )


def test_missing_schedule_session_fails_closed_instead_of_carrying_exposure() -> None:
    with pytest.raises(PerformanceUnavailable, match="each session requires"):
        calculate_performance(
            [bar(0), bar(1), bar(2)],
            [exposure(0, "1"), exposure(2, "1")],
        )


def test_misaligned_dates_fail_closed_even_when_input_lengths_match() -> None:
    with pytest.raises(PerformanceUnavailable, match="misaligned"):
        calculate_performance(
            [bar(0), bar(1)],
            [exposure(0, "1"), exposure(2, "1")],
        )


def test_duplicate_or_out_of_order_sessions_fail_closed() -> None:
    duplicate = bar(0)
    with pytest.raises(PerformanceUnavailable, match="strictly increasing"):
        calculate_performance(
            [duplicate, duplicate],
            [exposure(0, "1"), exposure(1, "1")],
        )


@pytest.mark.parametrize("value", ["-0.01", "4.8501"])
def test_exposure_outside_strategy_bounds_is_rejected(value: str) -> None:
    with pytest.raises(PerformanceUnavailable, match="between 0 and 4.85"):
        exposure(0, value)


def test_empty_history_and_schedule_fail_closed() -> None:
    with pytest.raises(PerformanceUnavailable, match="history is missing"):
        calculate_performance([], [])


def test_outputs_are_typed_immutable_tuples() -> None:
    result = calculate_performance([bar(0)], [exposure(0, "1")])

    assert isinstance(result.points, tuple)
    with pytest.raises(FrozenInstanceError):
        result.points[0].strategy_value = Decimal("0")  # type: ignore[misc]


def test_datetime_is_not_accepted_as_a_session_date() -> None:
    with pytest.raises(PerformanceUnavailable, match="must be a date"):
        SpyOhlcBar(
            session_date=datetime(2024, 1, 2),
            open_price=Decimal("100"),
            high_price=Decimal("100"),
            low_price=Decimal("100"),
            close_price=Decimal("100"),
        )
