"""Deterministic SPY-signaled ES/MES strategy domain API."""

from .calculation import (
    MarketRegime,
    RegimeTransition,
    StrategyMetrics,
    StrategyReplay,
    StrategySnapshot,
    VolatilityState,
    calculate_strategy,
    realized_volatility,
    replay_strategy,
)
from .errors import (
    CalculationUnavailable,
    DomainError,
    PerformanceUnavailable,
    ScheduleUnavailable,
    SizingUnavailable,
)
from .performance import (
    ExposureSchedulePoint,
    PerformancePoint,
    PerformanceSeries,
    SpyOhlcBar,
    calculate_performance,
    exposure_schedule_from_replay,
)
from .schedule import (
    ScheduledTargetInstruction,
    ScheduleState,
    TargetSignal,
    XnysCalendar,
    is_review_session,
    next_review_close,
    next_session_open,
    reconstruct_schedule,
    schedule_target,
)
from .sizing import FuturesSizingResult, size_futures
from .types import DailyBar, StrategyConfig, Weekday

__all__ = [
    "CalculationUnavailable",
    "DailyBar",
    "DomainError",
    "ExposureSchedulePoint",
    "FuturesSizingResult",
    "MarketRegime",
    "PerformancePoint",
    "PerformanceSeries",
    "PerformanceUnavailable",
    "RegimeTransition",
    "ScheduleState",
    "ScheduleUnavailable",
    "ScheduledTargetInstruction",
    "SizingUnavailable",
    "SpyOhlcBar",
    "StrategyConfig",
    "StrategyMetrics",
    "StrategyReplay",
    "StrategySnapshot",
    "TargetSignal",
    "VolatilityState",
    "Weekday",
    "XnysCalendar",
    "calculate_performance",
    "calculate_strategy",
    "exposure_schedule_from_replay",
    "is_review_session",
    "next_review_close",
    "next_session_open",
    "realized_volatility",
    "reconstruct_schedule",
    "replay_strategy",
    "schedule_target",
    "size_futures",
]
