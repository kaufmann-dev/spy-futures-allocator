"""SQLite connection and fresh v1 schema management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

_MIGRATION_1 = r"""
CREATE TABLE app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    strategy_amount_usd TEXT,
    current_es_quantity INTEGER,
    current_mes_quantity INTEGER,
    ibkr_host TEXT NOT NULL,
    ibkr_port INTEGER NOT NULL CHECK (ibkr_port BETWEEN 1 AND 65535),
    ibkr_client_id INTEGER NOT NULL CHECK (ibkr_client_id >= 0),
    fast_sma_sessions INTEGER NOT NULL CHECK (fast_sma_sessions > 0),
    slow_sma_sessions INTEGER NOT NULL CHECK (slow_sma_sessions > 0),
    rebound_fraction TEXT NOT NULL,
    rv_window_sessions INTEGER NOT NULL CHECK (rv_window_sessions >= 2),
    annualization_sessions INTEGER NOT NULL CHECK (annualization_sessions > 0),
    rv_threshold TEXT NOT NULL,
    normal_exposure TEXT NOT NULL,
    high_vol_exposure TEXT NOT NULL,
    review_weekdays TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO app_settings (
    id, strategy_amount_usd, current_es_quantity, current_mes_quantity,
    ibkr_host, ibkr_port, ibkr_client_id,
    fast_sma_sessions, slow_sma_sessions, rebound_fraction,
    rv_window_sessions, annualization_sessions, rv_threshold,
    normal_exposure, high_vol_exposure, review_weekdays, updated_at
) VALUES (
    1, NULL, NULL, NULL,
    '127.0.0.1', 4002, 51,
    100, 500, '0.25',
    20, 252, '0.25',
    '1.60', '0.60', '0,3',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

CREATE TABLE contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    con_id INTEGER NOT NULL,
    symbol TEXT NOT NULL CHECK (symbol IN ('ES', 'MES')),
    local_symbol TEXT NOT NULL,
    trading_class TEXT,
    exchange TEXT NOT NULL,
    currency TEXT NOT NULL,
    last_trade_date TEXT NOT NULL,
    multiplier TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    UNIQUE (symbol, con_id, resolved_at)
);
CREATE INDEX contracts_symbol_resolved_idx
    ON contracts (symbol, resolved_at DESC, id DESC);

CREATE TABLE spy_bars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL UNIQUE,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT,
    completed_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX spy_bars_session_idx ON spy_bars (session_date DESC);

CREATE TABLE futures_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    con_id INTEGER NOT NULL,
    symbol TEXT NOT NULL CHECK (symbol IN ('ES', 'MES')),
    bid TEXT,
    ask TEXT,
    last TEXT,
    mark TEXT,
    market_data_type INTEGER,
    quoted_at TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX futures_quotes_symbol_received_idx
    ON futures_quotes (symbol, received_at DESC, id DESC);

CREATE TABLE strategy_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE strategy_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_fingerprint TEXT NOT NULL REFERENCES strategy_configs(fingerprint),
    session_date TEXT NOT NULL,
    spy_close TEXT NOT NULL,
    fast_sma TEXT NOT NULL,
    slow_sma TEXT NOT NULL,
    rv TEXT NOT NULL,
    regime_state TEXT NOT NULL CHECK (regime_state IN ('RISK_ON', 'RISK_OFF')),
    exit_armed INTEGER NOT NULL CHECK (exit_armed IN (0, 1)),
    post_exit_low TEXT,
    reentry_level TEXT,
    volatility_state TEXT NOT NULL CHECK (volatility_state IN ('NORMAL', 'HIGH')),
    target_exposure TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    UNIQUE (config_fingerprint, session_date)
);
CREATE INDEX strategy_snapshots_config_session_idx
    ON strategy_snapshots (config_fingerprint, session_date DESC);
CREATE INDEX strategy_snapshots_config_effective_idx
    ON strategy_snapshots (config_fingerprint, effective_at DESC);

CREATE TABLE recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('LIVE', 'STALE', 'UNAVAILABLE')),
    calculated_at TEXT NOT NULL,
    is_pending INTEGER NOT NULL CHECK (is_pending IN (0, 1)),
    config_fingerprint TEXT REFERENCES strategy_configs(fingerprint),
    es_quantity INTEGER CHECK (es_quantity IS NULL OR es_quantity >= 0),
    mes_quantity INTEGER CHECK (mes_quantity IS NULL OR mes_quantity >= 0),
    es_contract_con_id INTEGER,
    es_contract_local_symbol TEXT,
    es_mark TEXT,
    es_mark_at TEXT,
    es_multiplier TEXT,
    mes_contract_con_id INTEGER,
    mes_contract_local_symbol TEXT,
    mes_mark TEXT,
    mes_mark_at TEXT,
    mes_multiplier TEXT,
    strategy_amount_usd TEXT,
    target_exposure TEXT,
    target_notional TEXT,
    achieved_notional TEXT,
    achieved_exposure TEXT,
    target_shortfall TEXT,
    target_effective_at TEXT,
    reason TEXT
);
CREATE INDEX recommendations_current_idx
    ON recommendations (is_pending, calculated_at DESC, id DESC);

CREATE TABLE audit_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail_json TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX audit_entries_occurred_idx
    ON audit_entries (occurred_at DESC, id DESC);
"""


class Database:
    """Own short-lived SQLite connections for the allocator and auth store."""

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
                ("spy_futures_allocator", 1),
            ).fetchone()
            if applied is None:
                legacy = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_settings'"
                ).fetchone()
                if legacy is not None:
                    raise RuntimeError(
                        "SPY Futures Allocator requires a fresh database path; "
                        "the selected database already contains another application schema"
                    )
                connection.executescript(_MIGRATION_1)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (component, version, applied_at)
                    VALUES ('spy_futures_allocator', 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
