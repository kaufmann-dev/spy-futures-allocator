from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.auth import SqliteAuthStore
from app.db import ContractRecord, Database, MesQuoteRecord, Repository
from app.security import SecretBox
from app.services.runtime import (
    OPENAI_KEY_PURPOSE,
    ApplicationRuntime,
    RuntimeUnavailable,
)


def runtime(tmp_path: Path) -> tuple[ApplicationRuntime, Repository, SecretBox]:
    database_path = tmp_path / "runtime.sqlite3"
    repository = Repository(Database(database_path))
    repository.initialize()
    secret_box = SecretBox("settings-secret" * 4)
    return (
        ApplicationRuntime(
            repository,
            SqliteAuthStore(str(database_path)),
            secret_box,
        ),
        repository,
        secret_box,
    )


def test_openai_key_is_encrypted_and_never_returned_as_settings(tmp_path: Path) -> None:
    service, repository, secret_box = runtime(tmp_path)
    updated = service.save_settings(
        strategy_amount_usd=Decimal("100000"),
        current_mes_quantity=None,
        ibkr_host="tws.internal",
        ibkr_port=4002,
        ibkr_client_id=51,
        openai_api_key="sk-project-test-value-with-enough-length",
        detector_model="gpt-5.6-luna",
        verifier_model="gpt-5.6-terra",
    )
    assert updated.openai_api_key_ciphertext is not None
    assert b"sk-project" not in updated.openai_api_key_ciphertext
    assert (
        secret_box.decrypt(
            bytes(updated.openai_api_key_ciphertext).decode(),
            purpose=OPENAI_KEY_PURPOSE,
        )
        == "sk-project-test-value-with-enough-length"
    )
    assert repository.get_settings().openai_api_key_ciphertext == updated.openai_api_key_ciphertext


def test_recalculation_without_required_data_persists_unavailable(tmp_path: Path) -> None:
    service, repository, _ = runtime(tmp_path)
    service.recalculate()
    result = repository.latest_recommendation()
    assert result is not None
    assert result.status == "UNAVAILABLE"
    assert result.quantity is None


def test_delayed_mes_quote_cannot_produce_current_recommendation(tmp_path: Path) -> None:
    service, repository, _ = runtime(tmp_path)
    now = datetime.now(UTC)
    service.save_settings(
        strategy_amount_usd=Decimal("100000"),
        current_mes_quantity=2,
        ibkr_host="tws.internal",
        ibkr_port=4002,
        ibkr_client_id=51,
        openai_api_key=None,
        detector_model="gpt-5.6-luna",
        verifier_model="gpt-5.6-terra",
    )
    repository.append_contract(
        ContractRecord(
            con_id=123,
            symbol="MES",
            local_symbol="MESU6",
            exchange="CME",
            currency="USD",
            last_trade_date="20260918",
            multiplier=Decimal("5"),
            resolved_at=now,
        )
    )
    repository.append_mes_quote(
        MesQuoteRecord(
            con_id=123,
            bid=Decimal("6500"),
            ask=Decimal("6501"),
            last=Decimal("6500.5"),
            mark=Decimal("6500.5"),
            market_data_type=3,
            quoted_at=now,
            received_at=now,
        )
    )
    with pytest.raises(RuntimeUnavailable, match="not live"):
        service._persist_recommendation(
            now=now,
            target=Decimal("2"),
            final_target=Decimal("2"),
            effective_at=now,
            pending=False,
        )
    assert repository.latest_recommendation() is None
