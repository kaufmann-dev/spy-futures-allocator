"""Typed persistence operations for SPY Futures Allocator."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from .database import Database
from .models import (
    AppSettings,
    AuditEntry,
    ContractRecord,
    FuturesQuoteRecord,
    RecommendationRecord,
    SpyBarRecord,
    StrategyConfigRecord,
    StrategySnapshotRecord,
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _required_datetime(value: str) -> datetime:
    parsed = _datetime(value)
    if parsed is None:  # pragma: no cover - guarded by SQLite NOT NULL
        raise RuntimeError("stored datetime is unexpectedly null")
    return parsed


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _required_decimal(value: str) -> Decimal:
    parsed = _decimal(value)
    if parsed is None:  # pragma: no cover - guarded by SQLite NOT NULL
        raise RuntimeError("stored decimal is unexpectedly null")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _limit(value: int, maximum: int = 20_000) -> int:
    if value < 1:
        raise ValueError("limit must be positive")
    return min(value, maximum)


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:  # pragma: no cover - inserts assign row ids
        raise RuntimeError("SQLite did not return an inserted row id")
    return cursor.lastrowid


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    def get_settings(self) -> AppSettings:
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("application settings were not initialized")
        return self._settings(row)

    def save_settings(self, settings: AppSettings) -> AppSettings:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE app_settings SET
                    strategy_amount_usd = ?, current_es_quantity = ?,
                    current_mes_quantity = ?, ibkr_host = ?, ibkr_port = ?,
                    ibkr_client_id = ?, fast_sma_sessions = ?, slow_sma_sessions = ?,
                    rebound_fraction = ?, rv_window_sessions = ?,
                    annualization_sessions = ?, rv_threshold = ?, normal_exposure = ?,
                    high_vol_exposure = ?, review_weekdays = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    _decimal_text(settings.strategy_amount_usd),
                    settings.current_es_quantity,
                    settings.current_mes_quantity,
                    settings.ibkr_host,
                    settings.ibkr_port,
                    settings.ibkr_client_id,
                    settings.fast_sma_sessions,
                    settings.slow_sma_sessions,
                    _decimal_text(settings.rebound_fraction),
                    settings.rv_window_sessions,
                    settings.annualization_sessions,
                    _decimal_text(settings.rv_threshold),
                    _decimal_text(settings.normal_exposure),
                    _decimal_text(settings.high_vol_exposure),
                    ",".join(str(value) for value in settings.review_weekdays),
                    _iso(settings.updated_at),
                ),
            )
        return settings

    def update_settings(self, **changes: object) -> AppSettings:
        allowed = set(AppSettings.__dataclass_fields__) - {"updated_at"}
        unknown = changes.keys() - allowed
        if unknown:
            raise TypeError(f"unknown settings fields: {', '.join(sorted(unknown))}")
        changes["updated_at"] = datetime.now(UTC)
        return self.save_settings(replace(self.get_settings(), **cast(Any, changes)))

    def append_contract(self, record: ContractRecord) -> ContractRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO contracts (
                    con_id, symbol, local_symbol, trading_class, exchange, currency,
                    last_trade_date, multiplier, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.con_id,
                    record.symbol,
                    record.local_symbol,
                    record.trading_class,
                    record.exchange,
                    record.currency,
                    record.last_trade_date,
                    _decimal_text(record.multiplier),
                    _iso(record.resolved_at),
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    """SELECT id FROM contracts
                       WHERE symbol = ? AND con_id = ? AND resolved_at = ?""",
                    (record.symbol, record.con_id, _iso(record.resolved_at)),
                ).fetchone()
                assert row is not None
                return replace(record, id=int(row["id"]))
        return replace(record, id=_lastrowid(cursor))

    def latest_contract(self, symbol: str) -> ContractRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT * FROM contracts WHERE symbol = ?
                   ORDER BY resolved_at DESC, id DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        return None if row is None else self._contract(row)

    def upsert_spy_bar(self, record: SpyBarRecord) -> SpyBarRecord:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO spy_bars (
                    session_date, open, high, low, close, volume, completed_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_date) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low,
                    close = excluded.close, volume = excluded.volume,
                    completed_at = excluded.completed_at, fetched_at = excluded.fetched_at
                """,
                (
                    record.session_date.isoformat(),
                    _decimal_text(record.open),
                    _decimal_text(record.high),
                    _decimal_text(record.low),
                    _decimal_text(record.close),
                    _decimal_text(record.volume),
                    _iso(record.completed_at),
                    _iso(record.fetched_at),
                ),
            )
            row = connection.execute(
                "SELECT id FROM spy_bars WHERE session_date = ?",
                (record.session_date.isoformat(),),
            ).fetchone()
        assert row is not None
        return replace(record, id=int(row["id"]))

    def latest_spy_bars(self, limit: int = 10_000) -> list[SpyBarRecord]:
        bounded = _limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM spy_bars ORDER BY session_date DESC LIMIT ?
                ) ORDER BY session_date ASC
                """,
                (bounded,),
            ).fetchall()
        return [self._spy_bar(row) for row in rows]

    def append_futures_quote(self, record: FuturesQuoteRecord) -> FuturesQuoteRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO futures_quotes (
                    con_id, symbol, bid, ask, last, mark, market_data_type,
                    quoted_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.con_id,
                    record.symbol,
                    _decimal_text(record.bid),
                    _decimal_text(record.ask),
                    _decimal_text(record.last),
                    _decimal_text(record.mark),
                    record.market_data_type,
                    _iso(record.quoted_at) if record.quoted_at else None,
                    _iso(record.received_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_futures_quote(self, symbol: str) -> FuturesQuoteRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT * FROM futures_quotes WHERE symbol = ?
                   ORDER BY received_at DESC, id DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        return None if row is None else self._quote(row)

    def append_strategy_config(self, record: StrategyConfigRecord) -> StrategyConfigRecord:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO strategy_configs
                   (fingerprint, config_json, created_at) VALUES (?, ?, ?)""",
                (record.fingerprint, record.config_json, _iso(record.created_at)),
            )
            row = connection.execute(
                "SELECT id FROM strategy_configs WHERE fingerprint = ?",
                (record.fingerprint,),
            ).fetchone()
        assert row is not None
        return replace(record, id=int(row["id"]))

    def strategy_config(self, fingerprint: str) -> StrategyConfigRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_configs WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return None if row is None else self._strategy_config(row)

    def replace_strategy_snapshots(
        self, fingerprint: str, records: list[StrategySnapshotRecord]
    ) -> None:
        if any(record.config_fingerprint != fingerprint for record in records):
            raise ValueError("all strategy snapshots must use the selected configuration")
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM strategy_snapshots WHERE config_fingerprint = ?", (fingerprint,)
            )
            connection.executemany(
                """
                INSERT INTO strategy_snapshots (
                    config_fingerprint, session_date, spy_close, fast_sma, slow_sma,
                    rv, regime_state, exit_armed, post_exit_low, reentry_level,
                    volatility_state, target_exposure, effective_at, calculated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.config_fingerprint,
                        item.session_date.isoformat(),
                        _decimal_text(item.spy_close),
                        _decimal_text(item.fast_sma),
                        _decimal_text(item.slow_sma),
                        _decimal_text(item.rv),
                        item.regime_state,
                        int(item.exit_armed),
                        _decimal_text(item.post_exit_low),
                        _decimal_text(item.reentry_level),
                        item.volatility_state,
                        _decimal_text(item.target_exposure),
                        _iso(item.effective_at),
                        _iso(item.calculated_at),
                    )
                    for item in records
                ],
            )

    def strategy_snapshots(
        self, fingerprint: str, limit: int = 10_000
    ) -> list[StrategySnapshotRecord]:
        bounded = _limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM strategy_snapshots WHERE config_fingerprint = ?
                    ORDER BY session_date DESC LIMIT ?
                ) ORDER BY session_date ASC
                """,
                (fingerprint, bounded),
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def current_strategy_snapshot(
        self, fingerprint: str, at: datetime
    ) -> StrategySnapshotRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_snapshots
                   WHERE config_fingerprint = ? AND effective_at <= ?
                   ORDER BY effective_at DESC, session_date DESC LIMIT 1""",
                (fingerprint, _iso(at)),
            ).fetchone()
        return None if row is None else self._snapshot(row)

    def pending_strategy_snapshot(
        self, fingerprint: str, at: datetime
    ) -> StrategySnapshotRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT * FROM strategy_snapshots
                   WHERE config_fingerprint = ? AND effective_at > ?
                   ORDER BY effective_at ASC, session_date ASC LIMIT 1""",
                (fingerprint, _iso(at)),
            ).fetchone()
        return None if row is None else self._snapshot(row)

    def append_recommendation(self, record: RecommendationRecord) -> RecommendationRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recommendations (
                    status, calculated_at, is_pending, config_fingerprint,
                    es_quantity, mes_quantity,
                    es_contract_con_id, es_contract_local_symbol, es_mark, es_mark_at,
                    es_multiplier, mes_contract_con_id, mes_contract_local_symbol,
                    mes_mark, mes_mark_at, mes_multiplier, strategy_amount_usd,
                    target_exposure, target_notional, achieved_notional,
                    achieved_exposure, target_shortfall, target_effective_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.status,
                    _iso(record.calculated_at),
                    int(record.is_pending),
                    record.config_fingerprint,
                    record.es_quantity,
                    record.mes_quantity,
                    record.es_contract_con_id,
                    record.es_contract_local_symbol,
                    _decimal_text(record.es_mark),
                    _iso(record.es_mark_at) if record.es_mark_at else None,
                    _decimal_text(record.es_multiplier),
                    record.mes_contract_con_id,
                    record.mes_contract_local_symbol,
                    _decimal_text(record.mes_mark),
                    _iso(record.mes_mark_at) if record.mes_mark_at else None,
                    _decimal_text(record.mes_multiplier),
                    _decimal_text(record.strategy_amount_usd),
                    _decimal_text(record.target_exposure),
                    _decimal_text(record.target_notional),
                    _decimal_text(record.achieved_notional),
                    _decimal_text(record.achieved_exposure),
                    _decimal_text(record.target_shortfall),
                    _iso(record.target_effective_at) if record.target_effective_at else None,
                    record.reason,
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_recommendation(self, *, pending: bool = False) -> RecommendationRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT * FROM recommendations WHERE is_pending = ?
                   ORDER BY calculated_at DESC, id DESC LIMIT 1""",
                (int(pending),),
            ).fetchone()
        return None if row is None else self._recommendation(row)

    def recommendation_history(self, limit: int = 100) -> list[RecommendationRecord]:
        bounded = _limit(limit, 500)
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT * FROM recommendations WHERE is_pending = 0
                   ORDER BY calculated_at DESC, id DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        return [self._recommendation(row) for row in rows]

    def append_audit(self, record: AuditEntry) -> AuditEntry:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO audit_entries (action, detail_json, occurred_at) VALUES (?, ?, ?)",
                (record.action, record.detail_json, _iso(record.occurred_at)),
            )
        return replace(record, id=_lastrowid(cursor))

    def audit_history(self, limit: int = 100) -> list[AuditEntry]:
        bounded = _limit(limit, 500)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_entries ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [
            AuditEntry(
                id=int(row["id"]),
                action=str(row["action"]),
                detail_json=cast(str | None, row["detail_json"]),
                occurred_at=_required_datetime(str(row["occurred_at"])),
            )
            for row in rows
        ]

    @staticmethod
    def _settings(row: sqlite3.Row) -> AppSettings:
        weekdays = tuple(int(value) for value in str(row["review_weekdays"]).split(","))
        return AppSettings(
            strategy_amount_usd=_decimal(cast(str | None, row["strategy_amount_usd"])),
            current_es_quantity=cast(int | None, row["current_es_quantity"]),
            current_mes_quantity=cast(int | None, row["current_mes_quantity"]),
            ibkr_host=str(row["ibkr_host"]),
            ibkr_port=int(row["ibkr_port"]),
            ibkr_client_id=int(row["ibkr_client_id"]),
            fast_sma_sessions=int(row["fast_sma_sessions"]),
            slow_sma_sessions=int(row["slow_sma_sessions"]),
            rebound_fraction=_required_decimal(str(row["rebound_fraction"])),
            rv_window_sessions=int(row["rv_window_sessions"]),
            annualization_sessions=int(row["annualization_sessions"]),
            rv_threshold=_required_decimal(str(row["rv_threshold"])),
            normal_exposure=_required_decimal(str(row["normal_exposure"])),
            high_vol_exposure=_required_decimal(str(row["high_vol_exposure"])),
            review_weekdays=weekdays,
            updated_at=_required_datetime(str(row["updated_at"])),
        )

    @staticmethod
    def _contract(row: sqlite3.Row) -> ContractRecord:
        return ContractRecord(
            id=int(row["id"]),
            con_id=int(row["con_id"]),
            symbol=str(row["symbol"]),
            local_symbol=str(row["local_symbol"]),
            trading_class=cast(str | None, row["trading_class"]),
            exchange=str(row["exchange"]),
            currency=str(row["currency"]),
            last_trade_date=str(row["last_trade_date"]),
            multiplier=_required_decimal(str(row["multiplier"])),
            resolved_at=_required_datetime(str(row["resolved_at"])),
        )

    @staticmethod
    def _spy_bar(row: sqlite3.Row) -> SpyBarRecord:
        return SpyBarRecord(
            id=int(row["id"]),
            session_date=date.fromisoformat(str(row["session_date"])),
            open=_required_decimal(str(row["open"])),
            high=_required_decimal(str(row["high"])),
            low=_required_decimal(str(row["low"])),
            close=_required_decimal(str(row["close"])),
            volume=_decimal(cast(str | None, row["volume"])),
            completed_at=_required_datetime(str(row["completed_at"])),
            fetched_at=_required_datetime(str(row["fetched_at"])),
        )

    @staticmethod
    def _quote(row: sqlite3.Row) -> FuturesQuoteRecord:
        return FuturesQuoteRecord(
            id=int(row["id"]),
            con_id=int(row["con_id"]),
            symbol=str(row["symbol"]),
            bid=_decimal(cast(str | None, row["bid"])),
            ask=_decimal(cast(str | None, row["ask"])),
            last=_decimal(cast(str | None, row["last"])),
            mark=_decimal(cast(str | None, row["mark"])),
            market_data_type=cast(int | None, row["market_data_type"]),
            quoted_at=_datetime(cast(str | None, row["quoted_at"])),
            received_at=_required_datetime(str(row["received_at"])),
        )

    @staticmethod
    def _strategy_config(row: sqlite3.Row) -> StrategyConfigRecord:
        return StrategyConfigRecord(
            id=int(row["id"]),
            fingerprint=str(row["fingerprint"]),
            config_json=str(row["config_json"]),
            created_at=_required_datetime(str(row["created_at"])),
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> StrategySnapshotRecord:
        return StrategySnapshotRecord(
            id=int(row["id"]),
            config_fingerprint=str(row["config_fingerprint"]),
            session_date=date.fromisoformat(str(row["session_date"])),
            spy_close=_required_decimal(str(row["spy_close"])),
            fast_sma=_required_decimal(str(row["fast_sma"])),
            slow_sma=_required_decimal(str(row["slow_sma"])),
            rv=_required_decimal(str(row["rv"])),
            regime_state=str(row["regime_state"]),
            exit_armed=bool(row["exit_armed"]),
            post_exit_low=_decimal(cast(str | None, row["post_exit_low"])),
            reentry_level=_decimal(cast(str | None, row["reentry_level"])),
            volatility_state=str(row["volatility_state"]),
            target_exposure=_required_decimal(str(row["target_exposure"])),
            effective_at=_required_datetime(str(row["effective_at"])),
            calculated_at=_required_datetime(str(row["calculated_at"])),
        )

    @staticmethod
    def _recommendation(row: sqlite3.Row) -> RecommendationRecord:
        return RecommendationRecord(
            id=int(row["id"]),
            status=str(row["status"]),
            calculated_at=_required_datetime(str(row["calculated_at"])),
            is_pending=bool(row["is_pending"]),
            config_fingerprint=cast(str | None, row["config_fingerprint"]),
            es_quantity=cast(int | None, row["es_quantity"]),
            mes_quantity=cast(int | None, row["mes_quantity"]),
            es_contract_con_id=cast(int | None, row["es_contract_con_id"]),
            es_contract_local_symbol=cast(str | None, row["es_contract_local_symbol"]),
            es_mark=_decimal(cast(str | None, row["es_mark"])),
            es_mark_at=_datetime(cast(str | None, row["es_mark_at"])),
            es_multiplier=_decimal(cast(str | None, row["es_multiplier"])),
            mes_contract_con_id=cast(int | None, row["mes_contract_con_id"]),
            mes_contract_local_symbol=cast(str | None, row["mes_contract_local_symbol"]),
            mes_mark=_decimal(cast(str | None, row["mes_mark"])),
            mes_mark_at=_datetime(cast(str | None, row["mes_mark_at"])),
            mes_multiplier=_decimal(cast(str | None, row["mes_multiplier"])),
            strategy_amount_usd=_decimal(cast(str | None, row["strategy_amount_usd"])),
            target_exposure=_decimal(cast(str | None, row["target_exposure"])),
            target_notional=_decimal(cast(str | None, row["target_notional"])),
            achieved_notional=_decimal(cast(str | None, row["achieved_notional"])),
            achieved_exposure=_decimal(cast(str | None, row["achieved_exposure"])),
            target_shortfall=_decimal(cast(str | None, row["target_shortfall"])),
            target_effective_at=_datetime(cast(str | None, row["target_effective_at"])),
            reason=cast(str | None, row["reason"]),
        )
