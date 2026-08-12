"""Typed, bounded persistence operations for application data."""

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
    EventCaseRecord,
    EventDocumentRecord,
    EventEvidenceRecord,
    EventModelRunRecord,
    EventStateRecord,
    MesQuoteRecord,
    RecommendationRecord,
    ScheduledTargetRecord,
    SignalSnapshotRecord,
    SpyBarRecord,
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _required_datetime(value: str) -> datetime:
    parsed = _datetime(value)
    if parsed is None:  # pragma: no cover - guarded by a NOT NULL constraint
        raise RuntimeError("stored datetime is unexpectedly null")
    return parsed


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _bounded_limit(limit: int, *, maximum: int = 500) -> int:
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, maximum)


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:  # pragma: no cover - INSERT always assigns one
        raise RuntimeError("SQLite did not return an inserted row id")
    return cursor.lastrowid


class Repository:
    """The only persistence surface used by calculation and request services."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    # Settings are a mutable singleton; history-worthy changes are written to
    # the separate audit log by the calling settings service.
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
                    strategy_amount_usd = ?, current_mes_quantity = ?,
                    ibkr_host = ?, ibkr_port = ?, ibkr_client_id = ?,
                    openai_api_key_ciphertext = ?, detector_model = ?,
                    verifier_model = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    _decimal_text(settings.strategy_amount_usd),
                    settings.current_mes_quantity,
                    settings.ibkr_host,
                    settings.ibkr_port,
                    settings.ibkr_client_id,
                    settings.openai_api_key_ciphertext,
                    settings.detector_model,
                    settings.verifier_model,
                    _iso(settings.updated_at),
                ),
            )
        return settings

    def update_settings(self, **changes: object) -> AppSettings:
        allowed = {
            "strategy_amount_usd",
            "current_mes_quantity",
            "ibkr_host",
            "ibkr_port",
            "ibkr_client_id",
            "openai_api_key_ciphertext",
            "detector_model",
            "verifier_model",
            "updated_at",
        }
        unknown = changes.keys() - allowed
        if unknown:
            raise TypeError(f"unknown settings fields: {', '.join(sorted(unknown))}")
        if "updated_at" not in changes:
            changes["updated_at"] = datetime.now(UTC)
        return self.save_settings(replace(self.get_settings(), **cast(Any, changes)))

    def append_contract(self, record: ContractRecord) -> ContractRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO contracts (
                    con_id, symbol, local_symbol, trading_class, exchange,
                    currency, last_trade_date, multiplier, resolved_at
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
            record_id = cursor.lastrowid
            if not record_id:
                row = connection.execute(
                    "SELECT id FROM contracts WHERE con_id = ? AND resolved_at = ?",
                    (record.con_id, _iso(record.resolved_at)),
                ).fetchone()
                record_id = row["id"]
        return replace(record, id=int(record_id))

    def latest_contract(self) -> ContractRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM contracts ORDER BY resolved_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._contract(row)

    def append_spy_bar(self, record: SpyBarRecord) -> SpyBarRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO spy_bars (
                    session_date, open, high, low, close, volume,
                    completed_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            record_id = cursor.lastrowid
            if not record_id:
                row = connection.execute(
                    "SELECT id FROM spy_bars WHERE session_date = ? AND fetched_at = ?",
                    (record.session_date.isoformat(), _iso(record.fetched_at)),
                ).fetchone()
                record_id = row["id"]
        return replace(record, id=int(record_id))

    def latest_spy_bars(self, limit: int = 260) -> list[SpyBarRecord]:
        limit = _bounded_limit(limit, maximum=1000)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY session_date
                        ORDER BY fetched_at DESC, id DESC
                    ) AS revision_rank
                    FROM spy_bars
                )
                WHERE revision_rank = 1
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._spy_bar(row) for row in reversed(rows)]

    def latest_spy_bar(self) -> SpyBarRecord | None:
        rows = self.latest_spy_bars(1)
        return rows[0] if rows else None

    def append_mes_quote(self, record: MesQuoteRecord) -> MesQuoteRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO mes_quotes (
                    con_id, bid, ask, last, mark, market_data_type,
                    quoted_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.con_id,
                    _decimal_text(record.bid),
                    _decimal_text(record.ask),
                    _decimal_text(record.last),
                    _decimal_text(record.mark),
                    record.market_data_type,
                    None if record.quoted_at is None else _iso(record.quoted_at),
                    _iso(record.received_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_mes_quote(self) -> MesQuoteRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM mes_quotes ORDER BY received_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._mes_quote(row)

    def append_signal_snapshot(self, record: SignalSnapshotRecord) -> SignalSnapshotRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO signal_snapshots (
                    session_date, spy_close, sma200, distance_above_sma200,
                    daily_return, rv20, prior_20_session_high, dd20, base,
                    bonus, volatility_cap, raw_target, shadow_raw_target,
                    calculated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_date.isoformat(),
                    _decimal_text(record.spy_close),
                    _decimal_text(record.sma200),
                    _decimal_text(record.distance_above_sma200),
                    _decimal_text(record.daily_return),
                    _decimal_text(record.rv20),
                    _decimal_text(record.prior_20_session_high),
                    _decimal_text(record.dd20),
                    _decimal_text(record.base),
                    _decimal_text(record.bonus),
                    _decimal_text(record.volatility_cap),
                    _decimal_text(record.raw_target),
                    _decimal_text(record.shadow_raw_target),
                    _iso(record.calculated_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_signal_snapshot(self) -> SignalSnapshotRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM signal_snapshots ORDER BY calculated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._signal_snapshot(row)

    def latest_signal_snapshots(self, limit: int = 260) -> list[SignalSnapshotRecord]:
        """Return the latest immutable revision for each recent session chronologically."""

        limit = _bounded_limit(limit, maximum=1000)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY session_date
                        ORDER BY calculated_at DESC, id DESC
                    ) AS revision_rank
                    FROM signal_snapshots
                )
                WHERE revision_rank = 1
                ORDER BY session_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._signal_snapshot(row) for row in reversed(rows)]

    def signal_snapshot_history(self, limit: int = 260) -> list[SignalSnapshotRecord]:
        """Return recent raw-target snapshots in chronological order."""

        limit = _bounded_limit(limit, maximum=2000)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM signal_snapshots
                ORDER BY session_date DESC, calculated_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._signal_snapshot(row) for row in reversed(rows)]

    def signal_snapshots_for_session(self, session_date: date) -> list[SignalSnapshotRecord]:
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM signal_snapshots
                WHERE session_date = ?
                ORDER BY calculated_at DESC, id DESC
                """,
                (session_date.isoformat(),),
            ).fetchall()
        return [self._signal_snapshot(row) for row in rows]

    def append_scheduled_target(self, record: ScheduledTargetRecord) -> ScheduledTargetRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scheduled_targets (
                    signal_session_date, target, effective_at, state,
                    calculated_at, became_effective_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.signal_session_date.isoformat(),
                    _decimal_text(record.target),
                    _iso(record.effective_at),
                    record.state,
                    _iso(record.calculated_at),
                    None
                    if record.became_effective_at is None
                    else _iso(record.became_effective_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def set_scheduled_target_state(
        self,
        target_id: int,
        state: str,
        *,
        became_effective_at: datetime | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_targets
                SET state = ?, became_effective_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    None if became_effective_at is None else _iso(became_effective_at),
                    target_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"scheduled target {target_id} does not exist")

    def current_scheduled_target(self, at: datetime | None = None) -> ScheduledTargetRecord | None:
        at = at or datetime.now(UTC)
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM scheduled_targets
                WHERE state = 'EFFECTIVE' AND effective_at <= ?
                ORDER BY effective_at DESC, id DESC LIMIT 1
                """,
                (_iso(at),),
            ).fetchone()
        return None if row is None else self._scheduled_target(row)

    def pending_scheduled_target(self, at: datetime | None = None) -> ScheduledTargetRecord | None:
        at = at or datetime.now(UTC)
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM scheduled_targets
                WHERE state = 'PENDING' AND effective_at > ?
                ORDER BY effective_at ASC, id ASC LIMIT 1
                """,
                (_iso(at),),
            ).fetchone()
        return None if row is None else self._scheduled_target(row)

    def scheduled_targets_for_signal(
        self, signal_session_date: date
    ) -> list[ScheduledTargetRecord]:
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_targets
                WHERE signal_session_date = ?
                ORDER BY calculated_at DESC, id DESC
                """,
                (signal_session_date.isoformat(),),
            ).fetchall()
        return [self._scheduled_target(row) for row in rows]

    def append_recommendation(self, record: RecommendationRecord) -> RecommendationRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recommendations (
                    status, calculated_at, is_pending, quantity,
                    contract_con_id, contract_local_symbol, strategy_amount_usd,
                    mes_mark, mes_mark_at, mes_multiplier, scheduled_target,
                    event_cap, final_target, contract_notional, target_notional,
                    achieved_notional, achieved_leverage, target_shortfall,
                    target_effective_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.status,
                    _iso(record.calculated_at),
                    int(record.is_pending),
                    record.quantity,
                    record.contract_con_id,
                    record.contract_local_symbol,
                    _decimal_text(record.strategy_amount_usd),
                    _decimal_text(record.mes_mark),
                    None if record.mes_mark_at is None else _iso(record.mes_mark_at),
                    _decimal_text(record.mes_multiplier),
                    _decimal_text(record.scheduled_target),
                    _decimal_text(record.event_cap),
                    _decimal_text(record.final_target),
                    _decimal_text(record.contract_notional),
                    _decimal_text(record.target_notional),
                    _decimal_text(record.achieved_notional),
                    _decimal_text(record.achieved_leverage),
                    _decimal_text(record.target_shortfall),
                    None
                    if record.target_effective_at is None
                    else _iso(record.target_effective_at),
                    record.reason,
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_recommendation(self, *, pending: bool = False) -> RecommendationRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM recommendations
                WHERE is_pending = ?
                ORDER BY calculated_at DESC, id DESC LIMIT 1
                """,
                (int(pending),),
            ).fetchone()
        return None if row is None else self._recommendation(row)

    def recommendation_history(self, limit: int = 25) -> list[RecommendationRecord]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recommendations
                WHERE is_pending = 0
                ORDER BY calculated_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._recommendation(row) for row in rows]

    def upsert_event_case(self, record: EventCaseRecord) -> EventCaseRecord:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO event_cases (
                    event_key, event_type, original_timestamp, window_start,
                    window_end, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    event_type = excluded.event_type,
                    original_timestamp = excluded.original_timestamp,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    updated_at = excluded.updated_at
                """,
                (
                    record.event_key,
                    record.event_type,
                    _iso(record.original_timestamp),
                    _iso(record.window_start),
                    _iso(record.window_end),
                    _iso(record.created_at),
                    _iso(record.updated_at),
                ),
            )
            row = connection.execute(
                "SELECT id, created_at FROM event_cases WHERE event_key = ?",
                (record.event_key,),
            ).fetchone()
        return replace(record, id=int(row["id"]), created_at=_required_datetime(row["created_at"]))

    def event_case(self, event_case_id: int) -> EventCaseRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM event_cases WHERE id = ?", (event_case_id,)
            ).fetchone()
        return None if row is None else self._event_case(row)

    def event_case_by_key(self, event_key: str) -> EventCaseRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM event_cases WHERE event_key = ?", (event_key,)
            ).fetchone()
        return None if row is None else self._event_case(row)

    def latest_event_case(self) -> EventCaseRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM event_cases ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._event_case(row)

    def append_event_model_run(self, record: EventModelRunRecord) -> EventModelRunRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO event_model_runs (
                    event_case_id, role, model, response_id, result,
                    structured_output_json, error, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_case_id,
                    record.role,
                    record.model,
                    record.response_id,
                    record.result,
                    record.structured_output_json,
                    record.error,
                    _iso(record.started_at),
                    _iso(record.completed_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_event_model_run(
        self,
        role: str,
        *,
        event_case_id: int | None = None,
    ) -> EventModelRunRecord | None:
        query = "SELECT * FROM event_model_runs WHERE role = ?"
        parameters: list[object] = [role]
        if event_case_id is not None:
            query += " AND event_case_id = ?"
            parameters.append(event_case_id)
        query += " ORDER BY completed_at DESC, id DESC LIMIT 1"
        with self.database.read() as connection:
            row = connection.execute(query, parameters).fetchone()
        return None if row is None else self._event_model_run(row)

    def event_model_runs(
        self,
        *,
        event_case_id: int | None = None,
        limit: int = 100,
    ) -> list[EventModelRunRecord]:
        limit = _bounded_limit(limit)
        query = "SELECT * FROM event_model_runs"
        parameters: list[object] = []
        if event_case_id is not None:
            query += " WHERE event_case_id = ?"
            parameters.append(event_case_id)
        query += " ORDER BY completed_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self.database.read() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._event_model_run(row) for row in rows]

    def append_event_document(self, record: EventDocumentRecord) -> EventDocumentRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO event_documents (
                    event_case_id, model_run_id, url, title, publisher,
                    source_class, published_at, retrieved_at, content_hash, excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_case_id,
                    record.model_run_id,
                    record.url,
                    record.title,
                    record.publisher,
                    record.source_class,
                    None if record.published_at is None else _iso(record.published_at),
                    _iso(record.retrieved_at),
                    record.content_hash,
                    record.excerpt,
                ),
            )
            record_id = cursor.lastrowid
            if not record_id:
                row = connection.execute(
                    """
                    SELECT id FROM event_documents
                    WHERE event_case_id IS ? AND url = ? AND content_hash = ?
                    """,
                    (record.event_case_id, record.url, record.content_hash),
                ).fetchone()
                record_id = row["id"]
        return replace(record, id=int(record_id))

    def event_documents(self, event_case_id: int, limit: int = 100) -> list[EventDocumentRecord]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_documents
                WHERE event_case_id = ?
                ORDER BY retrieved_at DESC, id DESC LIMIT ?
                """,
                (event_case_id, limit),
            ).fetchall()
        return [self._event_document(row) for row in rows]

    def event_documents_for_model_run(
        self, model_run_id: int, limit: int = 100
    ) -> list[EventDocumentRecord]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_documents
                WHERE model_run_id = ?
                ORDER BY retrieved_at DESC, id DESC LIMIT ?
                """,
                (model_run_id, limit),
            ).fetchall()
        return [self._event_document(row) for row in rows]

    def append_event_evidence(self, record: EventEvidenceRecord) -> EventEvidenceRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO event_evidence (
                    event_case_id, document_id, model_run_id, criterion_key,
                    supported, contradiction, excerpt, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_case_id,
                    record.document_id,
                    record.model_run_id,
                    record.criterion_key,
                    int(record.supported),
                    int(record.contradiction),
                    record.excerpt,
                    _iso(record.recorded_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def event_evidence(self, event_case_id: int, limit: int = 100) -> list[EventEvidenceRecord]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_evidence
                WHERE event_case_id = ?
                ORDER BY recorded_at DESC, id DESC LIMIT ?
                """,
                (event_case_id, limit),
            ).fetchall()
        return [self._event_evidence(row) for row in rows]

    def event_evidence_for_model_run(
        self, model_run_id: int, limit: int = 100
    ) -> list[EventEvidenceRecord]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_evidence
                WHERE model_run_id = ?
                ORDER BY recorded_at DESC, id DESC LIMIT ?
                """,
                (model_run_id, limit),
            ).fetchall()
        return [self._event_evidence(row) for row in rows]

    def append_event_state(self, record: EventStateRecord) -> EventStateRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO event_state_transitions (
                    event_case_id, state, event_cap, event_drawdown,
                    lock_session, post_exit_low, recovery_level, recovery_sessions,
                    recovery_confirmed_at, release_at, last_evaluated_session,
                    reason, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_case_id,
                    record.state,
                    _decimal_text(record.event_cap),
                    _decimal_text(record.event_drawdown),
                    None if record.lock_session is None else record.lock_session.isoformat(),
                    _decimal_text(record.post_exit_low),
                    _decimal_text(record.recovery_level),
                    record.recovery_sessions,
                    None
                    if record.recovery_confirmed_at is None
                    else _iso(record.recovery_confirmed_at),
                    None if record.release_at is None else _iso(record.release_at),
                    None
                    if record.last_evaluated_session is None
                    else record.last_evaluated_session.isoformat(),
                    record.reason,
                    _iso(record.occurred_at),
                ),
            )
        return replace(record, id=_lastrowid(cursor))

    def latest_event_state(self) -> EventStateRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM event_state_transitions
                ORDER BY occurred_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return None if row is None else self._event_state(row)

    def event_state_history(self, event_case_id: int, limit: int = 100) -> list[EventStateRecord]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_state_transitions
                WHERE event_case_id = ?
                ORDER BY occurred_at DESC, id DESC LIMIT ?
                """,
                (event_case_id, limit),
            ).fetchall()
        return [self._event_state(row) for row in rows]

    def event_state_transition_history(self, limit: int = 100) -> list[EventStateRecord]:
        """Return recent transitions across all event cases in chronological order."""

        limit = _bounded_limit(limit, maximum=2000)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_state_transitions
                ORDER BY occurred_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._event_state(row) for row in reversed(rows)]

    def append_audit(self, record: AuditEntry) -> AuditEntry:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_entries (action, detail_json, occurred_at)
                VALUES (?, ?, ?)
                """,
                (record.action, record.detail_json, _iso(record.occurred_at)),
            )
        return replace(record, id=_lastrowid(cursor))

    def audit_history(self, limit: int = 100) -> list[AuditEntry]:
        limit = _bounded_limit(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_entries ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._audit(row) for row in rows]

    @staticmethod
    def _settings(row: sqlite3.Row) -> AppSettings:
        return AppSettings(
            strategy_amount_usd=_decimal(row["strategy_amount_usd"]),
            current_mes_quantity=row["current_mes_quantity"],
            ibkr_host=row["ibkr_host"],
            ibkr_port=row["ibkr_port"],
            ibkr_client_id=row["ibkr_client_id"],
            openai_api_key_ciphertext=row["openai_api_key_ciphertext"],
            detector_model=row["detector_model"],
            verifier_model=row["verifier_model"],
            updated_at=_required_datetime(row["updated_at"]),
        )

    @staticmethod
    def _contract(row: sqlite3.Row) -> ContractRecord:
        return ContractRecord(
            id=row["id"],
            con_id=row["con_id"],
            symbol=row["symbol"],
            local_symbol=row["local_symbol"],
            trading_class=row["trading_class"],
            exchange=row["exchange"],
            currency=row["currency"],
            last_trade_date=row["last_trade_date"],
            multiplier=Decimal(row["multiplier"]),
            resolved_at=_required_datetime(row["resolved_at"]),
        )

    @staticmethod
    def _spy_bar(row: sqlite3.Row) -> SpyBarRecord:
        return SpyBarRecord(
            id=row["id"],
            session_date=date.fromisoformat(row["session_date"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=_decimal(row["volume"]),
            completed_at=_required_datetime(row["completed_at"]),
            fetched_at=_required_datetime(row["fetched_at"]),
        )

    @staticmethod
    def _mes_quote(row: sqlite3.Row) -> MesQuoteRecord:
        return MesQuoteRecord(
            id=row["id"],
            con_id=row["con_id"],
            bid=_decimal(row["bid"]),
            ask=_decimal(row["ask"]),
            last=_decimal(row["last"]),
            mark=_decimal(row["mark"]),
            market_data_type=row["market_data_type"],
            quoted_at=_datetime(row["quoted_at"]),
            received_at=_required_datetime(row["received_at"]),
        )

    @staticmethod
    def _signal_snapshot(row: sqlite3.Row) -> SignalSnapshotRecord:
        return SignalSnapshotRecord(
            id=row["id"],
            session_date=date.fromisoformat(row["session_date"]),
            spy_close=Decimal(row["spy_close"]),
            sma200=Decimal(row["sma200"]),
            distance_above_sma200=Decimal(row["distance_above_sma200"]),
            daily_return=Decimal(row["daily_return"]),
            rv20=Decimal(row["rv20"]),
            prior_20_session_high=Decimal(row["prior_20_session_high"]),
            dd20=Decimal(row["dd20"]),
            base=Decimal(row["base"]),
            bonus=Decimal(row["bonus"]),
            volatility_cap=Decimal(row["volatility_cap"]),
            raw_target=Decimal(row["raw_target"]),
            shadow_raw_target=Decimal(row["shadow_raw_target"]),
            calculated_at=_required_datetime(row["calculated_at"]),
        )

    @staticmethod
    def _scheduled_target(row: sqlite3.Row) -> ScheduledTargetRecord:
        return ScheduledTargetRecord(
            id=row["id"],
            signal_session_date=date.fromisoformat(row["signal_session_date"]),
            target=Decimal(row["target"]),
            effective_at=_required_datetime(row["effective_at"]),
            state=row["state"],
            calculated_at=_required_datetime(row["calculated_at"]),
            became_effective_at=_datetime(row["became_effective_at"]),
        )

    @staticmethod
    def _recommendation(row: sqlite3.Row) -> RecommendationRecord:
        return RecommendationRecord(
            id=row["id"],
            status=row["status"],
            calculated_at=_required_datetime(row["calculated_at"]),
            is_pending=bool(row["is_pending"]),
            quantity=row["quantity"],
            contract_con_id=row["contract_con_id"],
            contract_local_symbol=row["contract_local_symbol"],
            strategy_amount_usd=_decimal(row["strategy_amount_usd"]),
            mes_mark=_decimal(row["mes_mark"]),
            mes_mark_at=_datetime(row["mes_mark_at"]),
            mes_multiplier=_decimal(row["mes_multiplier"]),
            scheduled_target=_decimal(row["scheduled_target"]),
            event_cap=_decimal(row["event_cap"]),
            final_target=_decimal(row["final_target"]),
            contract_notional=_decimal(row["contract_notional"]),
            target_notional=_decimal(row["target_notional"]),
            achieved_notional=_decimal(row["achieved_notional"]),
            achieved_leverage=_decimal(row["achieved_leverage"]),
            target_shortfall=_decimal(row["target_shortfall"]),
            target_effective_at=_datetime(row["target_effective_at"]),
            reason=row["reason"],
        )

    @staticmethod
    def _event_case(row: sqlite3.Row) -> EventCaseRecord:
        return EventCaseRecord(
            id=row["id"],
            event_key=row["event_key"],
            event_type=row["event_type"],
            original_timestamp=_required_datetime(row["original_timestamp"]),
            window_start=_required_datetime(row["window_start"]),
            window_end=_required_datetime(row["window_end"]),
            created_at=_required_datetime(row["created_at"]),
            updated_at=_required_datetime(row["updated_at"]),
        )

    @staticmethod
    def _event_model_run(row: sqlite3.Row) -> EventModelRunRecord:
        return EventModelRunRecord(
            id=row["id"],
            event_case_id=row["event_case_id"],
            role=row["role"],
            model=row["model"],
            response_id=row["response_id"],
            result=row["result"],
            structured_output_json=row["structured_output_json"],
            error=row["error"],
            started_at=_required_datetime(row["started_at"]),
            completed_at=_required_datetime(row["completed_at"]),
        )

    @staticmethod
    def _event_document(row: sqlite3.Row) -> EventDocumentRecord:
        return EventDocumentRecord(
            id=row["id"],
            event_case_id=row["event_case_id"],
            model_run_id=row["model_run_id"],
            url=row["url"],
            title=row["title"],
            publisher=row["publisher"],
            source_class=row["source_class"],
            published_at=_datetime(row["published_at"]),
            retrieved_at=_required_datetime(row["retrieved_at"]),
            content_hash=row["content_hash"],
            excerpt=row["excerpt"],
        )

    @staticmethod
    def _event_evidence(row: sqlite3.Row) -> EventEvidenceRecord:
        return EventEvidenceRecord(
            id=row["id"],
            event_case_id=row["event_case_id"],
            document_id=row["document_id"],
            model_run_id=row["model_run_id"],
            criterion_key=row["criterion_key"],
            supported=bool(row["supported"]),
            contradiction=bool(row["contradiction"]),
            excerpt=row["excerpt"],
            recorded_at=_required_datetime(row["recorded_at"]),
        )

    @staticmethod
    def _event_state(row: sqlite3.Row) -> EventStateRecord:
        return EventStateRecord(
            id=row["id"],
            event_case_id=row["event_case_id"],
            state=row["state"],
            event_cap=Decimal(row["event_cap"]),
            event_drawdown=_decimal(row["event_drawdown"]),
            lock_session=None
            if row["lock_session"] is None
            else date.fromisoformat(row["lock_session"]),
            post_exit_low=_decimal(row["post_exit_low"]),
            recovery_level=_decimal(row["recovery_level"]),
            recovery_sessions=row["recovery_sessions"],
            recovery_confirmed_at=_datetime(row["recovery_confirmed_at"]),
            release_at=_datetime(row["release_at"]),
            last_evaluated_session=(
                None
                if row["last_evaluated_session"] is None
                else date.fromisoformat(row["last_evaluated_session"])
            ),
            reason=row["reason"],
            occurred_at=_required_datetime(row["occurred_at"]),
        )

    @staticmethod
    def _audit(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            id=row["id"],
            action=row["action"],
            detail_json=row["detail_json"],
            occurred_at=_required_datetime(row["occurred_at"]),
        )
