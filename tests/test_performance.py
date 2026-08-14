from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.calculation import replay_strategy
from app.domain.errors import PerformanceUnavailable
from app.domain.performance import (
    ExposureSchedulePoint,
    SpyOhlcBar,
    calculate_performance,
    exposure_schedule_from_replay,
)
from app.domain.types import DailyBar, StrategyConfig


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


def exposure(offset: int, open_exposure: str) -> ExposureSchedulePoint:
    return ExposureSchedulePoint(
        session_date=date(2024, 1, 2) + timedelta(days=offset),
        open_exposure=Decimal(open_exposure),
    )


def test_constant_price_keeps_both_series_at_one_hundred_at_any_exposure() -> None:
    result = calculate_performance(
        [bar(0), bar(1), bar(2)],
        [exposure(0, "9"), exposure(1, "0"), exposure(2, "12.5")],
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
    assert point.close_exposure == Decimal("2")


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
        [exposure(0, "9"), exposure(1, "9"), exposure(2, "9")],
    )

    wiped_out = result.points[1]
    assert wiped_out.strategy_value == 0
    assert wiped_out.strategy_daily_return == -1
    assert wiped_out.strategy_drawdown == -1
    assert wiped_out.strategy_at_floor is True
    assert result.points[2].strategy_value == 0
    assert result.points[2].strategy_daily_return == 0
    assert result.strategy_max_drawdown == -1


def test_replay_targets_are_shifted_to_the_next_observed_session_open() -> None:
    sessions = (
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
    )
    adjusted = [
        DailyBar(day, Decimal(close))
        for day, close in zip(sessions, ("100", "100", "100", "120", "120", "120"), strict=True)
    ]
    selected = StrategyConfig(
        fast_sma_sessions=2,
        slow_sma_sessions=3,
        rv_window_sessions=2,
        annualization_sessions=4,
        rv_threshold=Decimal("0.10"),
    )
    replay = replay_strategy(adjusted, selected)

    schedule = exposure_schedule_from_replay(replay)

    assert tuple(point.session_date for point in schedule) == sessions[2:]
    assert tuple(point.open_exposure for point in schedule) == (
        Decimal("0"),
        Decimal("1.60"),
        Decimal("1.60"),
        Decimal("0.60"),
    )


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


def test_missing_or_misaligned_schedule_fails_closed() -> None:
    with pytest.raises(PerformanceUnavailable, match="each session requires"):
        calculate_performance(
            [bar(0), bar(1), bar(2)],
            [exposure(0, "1"), exposure(2, "1")],
        )
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


def test_negative_exposure_is_rejected_but_there_is_no_upper_cap() -> None:
    with pytest.raises(PerformanceUnavailable, match="nonnegative"):
        exposure(0, "-0.01")
    assert exposure(0, "100").open_exposure == Decimal("100")


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
