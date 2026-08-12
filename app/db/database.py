"""SQLite connection and migration management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

_MIGRATION_1 = r"""
CREATE TABLE IF NOT EXISTS app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    strategy_amount_usd TEXT,
    current_mes_quantity INTEGER,
    ibkr_host TEXT NOT NULL,
    ibkr_port INTEGER NOT NULL CHECK (ibkr_port BETWEEN 1 AND 65535),
    ibkr_client_id INTEGER NOT NULL CHECK (ibkr_client_id >= 0),
    openai_api_key_ciphertext BLOB,
    detector_model TEXT NOT NULL,
    verifier_model TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO app_settings (
    id, strategy_amount_usd, current_mes_quantity,
    ibkr_host, ibkr_port, ibkr_client_id,
    openai_api_key_ciphertext, detector_model, verifier_model, updated_at
) VALUES (
    1, NULL, NULL,
    '127.0.0.1', 4002, 51,
    NULL, 'gpt-5.6-luna', 'gpt-5.6-terra',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    con_id INTEGER NOT NULL,
    symbol TEXT NOT NULL CHECK (symbol = 'MES'),
    local_symbol TEXT NOT NULL,
    trading_class TEXT,
    exchange TEXT NOT NULL,
    currency TEXT NOT NULL,
    last_trade_date TEXT NOT NULL,
    multiplier TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    UNIQUE (con_id, resolved_at)
);
CREATE INDEX IF NOT EXISTS contracts_resolved_at_idx
    ON contracts (resolved_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS spy_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT,
    completed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (session_date, fetched_at)
);
CREATE INDEX IF NOT EXISTS spy_bars_session_idx
    ON spy_bars (session_date DESC, fetched_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS mes_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    con_id INTEGER NOT NULL,
    bid TEXT,
    ask TEXT,
    last TEXT,
    mark TEXT,
    market_data_type INTEGER,
    quoted_at TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mes_quotes_received_at_idx
    ON mes_quotes (received_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS signal_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    spy_close TEXT NOT NULL,
    sma200 TEXT NOT NULL,
    distance_above_sma200 TEXT NOT NULL,
    daily_return TEXT NOT NULL,
    rv20 TEXT NOT NULL,
    prior_20_session_high TEXT NOT NULL,
    dd20 TEXT NOT NULL,
    base TEXT NOT NULL,
    bonus TEXT NOT NULL,
    volatility_cap TEXT NOT NULL,
    raw_target TEXT NOT NULL,
    shadow_raw_target TEXT NOT NULL,
    calculated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signal_snapshots_calculated_idx
    ON signal_snapshots (calculated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS scheduled_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_session_date TEXT NOT NULL,
    target TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'EFFECTIVE', 'SKIPPED', 'SUPERSEDED')),
    calculated_at TEXT NOT NULL,
    became_effective_at TEXT
);
CREATE INDEX IF NOT EXISTS scheduled_targets_state_effective_idx
    ON scheduled_targets (state, effective_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('LIVE', 'STALE', 'UNAVAILABLE')),
    calculated_at TEXT NOT NULL,
    is_pending INTEGER NOT NULL CHECK (is_pending IN (0, 1)),
    quantity INTEGER CHECK (quantity IS NULL OR quantity >= 0),
    contract_con_id INTEGER,
    contract_local_symbol TEXT,
    strategy_amount_usd TEXT,
    mes_mark TEXT,
    mes_mark_at TEXT,
    mes_multiplier TEXT,
    scheduled_target TEXT,
    event_cap TEXT,
    final_target TEXT,
    contract_notional TEXT,
    target_notional TEXT,
    achieved_notional TEXT,
    achieved_leverage TEXT,
    target_shortfall TEXT,
    target_effective_at TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS recommendations_current_idx
    ON recommendations (is_pending, calculated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS event_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN ('PANDEMIC', 'MAJOR_INTERSTATE_WAR')),
    original_timestamp TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_case_id INTEGER REFERENCES event_cases(id),
    role TEXT NOT NULL CHECK (role IN ('DETECTOR', 'VERIFIER')),
    model TEXT NOT NULL,
    response_id TEXT,
    result TEXT NOT NULL,
    structured_output_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_model_runs_completed_idx
    ON event_model_runs (completed_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS event_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_case_id INTEGER REFERENCES event_cases(id),
    model_run_id INTEGER REFERENCES event_model_runs(id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_class TEXT NOT NULL,
    published_at TEXT,
    retrieved_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    excerpt TEXT,
    UNIQUE (event_case_id, url, content_hash)
);
CREATE INDEX IF NOT EXISTS event_documents_retrieved_idx
    ON event_documents (retrieved_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS event_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_case_id INTEGER REFERENCES event_cases(id),
    document_id INTEGER REFERENCES event_documents(id),
    model_run_id INTEGER REFERENCES event_model_runs(id),
    criterion_key TEXT NOT NULL,
    supported INTEGER NOT NULL CHECK (supported IN (0, 1)),
    contradiction INTEGER NOT NULL CHECK (contradiction IN (0, 1)),
    excerpt TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_evidence_case_idx
    ON event_evidence (event_case_id, recorded_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS event_state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_case_id INTEGER REFERENCES event_cases(id),
    state TEXT NOT NULL CHECK (state IN (
        'NO_EVENT', 'NEEDS_HUMAN_REVIEW', 'WINDOW_ACTIVE',
        'EVENT_LOCKED', 'RECOVERY_TRACKING', 'RECOVERY_CONFIRMED'
    )),
    event_cap TEXT NOT NULL,
    event_drawdown TEXT,
    lock_session TEXT,
    post_exit_low TEXT,
    recovery_level TEXT,
    recovery_sessions INTEGER NOT NULL DEFAULT 0 CHECK (recovery_sessions BETWEEN 0 AND 3),
    recovery_confirmed_at TEXT,
    release_at TEXT,
    last_evaluated_session TEXT,
    reason TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_state_transitions_occurred_idx
    ON event_state_transitions (occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS audit_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail_json TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_entries_occurred_idx
    ON audit_entries (occurred_at DESC, id DESC);
"""


class Database:
    """Owns short-lived SQLite connections and app-specific migrations.

    Each operation gets its own connection, which is safe for the scheduler,
    IBKR callback thread, and request workers.  Auth may create its own tables in
    the same file; the component-scoped migration ledger deliberately avoids
    taking ownership of SQLite's global ``user_version``.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._migration_lock = Lock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._migration_lock, self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    component TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    applied_at TEXT NOT NULL,
                    PRIMARY KEY (component, version)
                )
                """
            )
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE component = ? AND version = ?",
                ("app", 1),
            ).fetchone()
            if applied is None:
                connection.executescript(_MIGRATION_1)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (component, version, applied_at)
                    VALUES ('app', 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """
                )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
