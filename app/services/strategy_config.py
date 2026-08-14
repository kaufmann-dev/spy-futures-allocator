"""Canonical strategy configuration conversion and fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from app.db.models import AppSettings
from app.domain import StrategyConfig


def strategy_config_from_settings(settings: AppSettings) -> StrategyConfig:
    """Build the validated domain configuration selected by the user."""

    return StrategyConfig(
        fast_sma_sessions=settings.fast_sma_sessions,
        slow_sma_sessions=settings.slow_sma_sessions,
        rebound_fraction=settings.rebound_fraction,
        rv_window_sessions=settings.rv_window_sessions,
        annualization_sessions=settings.annualization_sessions,
        rv_threshold=settings.rv_threshold,
        normal_exposure=settings.normal_exposure,
        high_vol_exposure=settings.high_vol_exposure,
        review_weekdays=settings.review_weekdays,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def serialize_strategy_config(config: StrategyConfig) -> str:
    """Return the stable JSON representation used for persistence and hashing."""

    return json.dumps(
        _json_value(asdict(config)),
        sort_keys=True,
        separators=(",", ":"),
    )


def strategy_config_fingerprint(config: StrategyConfig) -> str:
    """Identify one exact replay configuration without exposing mutable state."""

    payload = serialize_strategy_config(config).encode()
    return hashlib.sha256(payload).hexdigest()
