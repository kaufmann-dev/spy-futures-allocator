"""SQLite persistence for one-time OIDC flows and opaque app sessions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .models import AuthSession, LoginFlow, LogoutFlow


class AuthStore(Protocol):
    def create_login_flow(self, flow: LoginFlow) -> None: ...

    def consume_login_flow(self, state_hash: str, consumed_at: int) -> LoginFlow | None: ...

    def create_session(self, session: AuthSession) -> None: ...

    def get_session(self, token_hash: str) -> AuthSession | None: ...

    def touch_session(self, token_hash: str, active_at: int) -> bool: ...

    def delete_session(self, token_hash: str) -> None: ...

    def create_logout_flow(self, flow: LogoutFlow) -> None: ...

    def consume_logout_flow(self, state_hash: str, consumed_at: int) -> LogoutFlow | None: ...

    def prune(self, before: int) -> None: ...


class SqliteAuthStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._memory_connection: sqlite3.Connection | None = None
        if database_path == ":memory:":
            self._memory_connection = self._new_connection()
        self.initialize()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self.database_path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            yield self._memory_connection
            return
        connection = self._new_connection()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oidc_flows (
                    state_hash TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    return_to TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_active_at INTEGER NOT NULL,
                    absolute_expires_at INTEGER NOT NULL,
                    encrypted_id_token BLOB NOT NULL,
                    id_token_nonce BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_expiry_idx
                    ON auth_sessions (absolute_expires_at, last_active_at);

                CREATE TABLE IF NOT EXISTS oidc_logout_flows (
                    state_hash TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                """
            )

    def create_login_flow(self, flow: LoginFlow) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO oidc_flows
                    (state_hash, nonce, code_verifier, return_to, created_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (flow.state_hash, flow.nonce, flow.code_verifier, flow.return_to, flow.created_at),
            )

    def consume_login_flow(self, state_hash: str, consumed_at: int) -> LoginFlow | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM oidc_flows WHERE state_hash = ? AND consumed_at IS NULL",
                (state_hash,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE oidc_flows SET consumed_at = ? "
                "WHERE state_hash = ? AND consumed_at IS NULL",
                (consumed_at, state_hash),
            )
            connection.execute("COMMIT")
            return LoginFlow(
                state_hash=row["state_hash"],
                nonce=row["nonce"],
                code_verifier=row["code_verifier"],
                return_to=row["return_to"],
                created_at=row["created_at"],
                consumed_at=consumed_at,
            )

    def create_session(self, session: AuthSession) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    token_hash, subject, created_at, last_active_at, absolute_expires_at,
                    encrypted_id_token, id_token_nonce
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.token_hash,
                    session.subject,
                    session.created_at,
                    session.last_active_at,
                    session.absolute_expires_at,
                    session.encrypted_id_token,
                    session.id_token_nonce,
                ),
            )

    def get_session(self, token_hash: str) -> AuthSession | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if row is None:
            return None
        return AuthSession(
            token_hash=row["token_hash"],
            subject=row["subject"],
            created_at=row["created_at"],
            last_active_at=row["last_active_at"],
            absolute_expires_at=row["absolute_expires_at"],
            encrypted_id_token=bytes(row["encrypted_id_token"]),
            id_token_nonce=bytes(row["id_token_nonce"]),
        )

    def touch_session(self, token_hash: str, active_at: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE auth_sessions SET last_active_at = ? WHERE token_hash = ?",
                (active_at, token_hash),
            )
            return cursor.rowcount == 1

    def delete_session(self, token_hash: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))

    def create_logout_flow(self, flow: LogoutFlow) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO oidc_logout_flows (state_hash, created_at, consumed_at) "
                "VALUES (?, ?, NULL)",
                (flow.state_hash, flow.created_at),
            )

    def consume_logout_flow(self, state_hash: str, consumed_at: int) -> LogoutFlow | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM oidc_logout_flows WHERE state_hash = ? AND consumed_at IS NULL",
                (state_hash,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE oidc_logout_flows SET consumed_at = ? "
                "WHERE state_hash = ? AND consumed_at IS NULL",
                (consumed_at, state_hash),
            )
            connection.execute("COMMIT")
            return LogoutFlow(
                state_hash=row["state_hash"],
                created_at=row["created_at"],
                consumed_at=consumed_at,
            )

    def prune(self, before: int) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM oidc_flows WHERE created_at < ?", (before,))
            connection.execute("DELETE FROM oidc_logout_flows WHERE created_at < ?", (before,))
            connection.execute("DELETE FROM auth_sessions WHERE absolute_expires_at < ?", (before,))
