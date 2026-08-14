from __future__ import annotations

import base64
import time
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from app.auth import AuthService, AuthSession
from app.auth.security import token_hash
from app.config import AppConfig
from app.main import (
    PRODUCT_ID,
    SettingsPayload,
    _chart_view,
    _csrf_token,
    _form_settings,
    create_app,
)
from app.services.runtime import ApplicationRuntime


def config(tmp_path: Path) -> AppConfig:
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    return AppConfig.model_validate(
        {
            "APP_ORIGIN": "https://spy-futures-allocator.example.test",
            "DATABASE_PATH": tmp_path / "app.sqlite3",
            "SESSION_SECRET": "s" * 64,
            "SETTINGS_ENCRYPTION_KEY": key,
            "OIDC_ISSUER_URL": "https://id.example.test",
            "OIDC_CLIENT_ID": "client-id",
            "OIDC_CLIENT_SECRET": "client-secret",
        }
    )


def client(tmp_path: Path) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(config(tmp_path))),
        base_url="https://spy-futures-allocator.example.test",
    )


def authenticated_app(tmp_path: Path) -> tuple[FastAPI, str, str]:
    selected_config = config(tmp_path)
    app = create_app(selected_config)
    service = cast(AuthService, app.state.auth_service)
    session_value = base64.urlsafe_b64encode(tmp_path.as_posix().encode()).decode()
    now = int(time.time())
    service.store.create_session(
        AuthSession(
            token_hash=token_hash(session_value),
            subject="test-user",
            created_at=now,
            last_active_at=now,
            absolute_expires_at=now + 3600,
            encrypted_id_token=b"test",
            id_token_nonce=b"test",
        )
    )
    csrf = _csrf_token(
        session_value,
        selected_config.session_secret.get_secret_value(),
    )
    return app, session_value, csrf


def settings_form(csrf: str) -> dict[str, object]:
    return {
        "csrf_token": csrf,
        "strategy_amount_usd": "100000.00",
        "current_es_quantity": "",
        "current_mes_quantity": "4",
        "ibkr_host": "gateway.internal",
        "ibkr_port": "4002",
        "ibkr_client_id": "51",
        "fast_sma_sessions": "100",
        "slow_sma_sessions": "500",
        "rebound_percent": "25",
        "rv_window_sessions": "20",
        "annualization_sessions": "252",
        "rv_threshold_percent": "25",
        "normal_exposure": "1.60",
        "high_vol_exposure": "0.60",
        "review_weekdays": ["3", "0"],
    }


def settings_payload(**overrides: object) -> SettingsPayload:
    values: dict[str, object] = {
        "strategy_amount_usd": "100000.00",
        "current_es_quantity": -1,
        "current_mes_quantity": 4,
        "ibkr_host": "gateway.internal",
        "ibkr_port": 4002,
        "ibkr_client_id": 51,
        "fast_sma_sessions": 100,
        "slow_sma_sessions": 500,
        "rebound_fraction": "0.25",
        "rv_window_sessions": 20,
        "annualization_sessions": 252,
        "rv_threshold": "0.25",
        "normal_exposure": "1.60",
        "high_vol_exposure": "0.60",
        "review_weekdays": [3, 0],
    }
    values.update(overrides)
    return SettingsPayload.model_validate(values)


def test_chart_view_allowlist_defaults_to_performance() -> None:
    assert _chart_view(None) == "performance"
    assert _chart_view("performance") == "performance"
    assert _chart_view("drawdowns") == "drawdowns"
    assert _chart_view("exposure") == "exposure"
    assert _chart_view("trend") == "trend"
    assert _chart_view("risk") == "risk"
    assert _chart_view("<script>") == "performance"


def test_settings_payload_normalizes_weekdays_and_signed_positions() -> None:
    payload = settings_payload()
    assert payload.review_weekdays == (0, 3)
    assert payload.current_es_quantity == -1
    assert payload.normal_exposure == Decimal("1.60")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"fast_sma_sessions": 500}, "fast_sma_sessions"),
        ({"high_vol_exposure": "1.60"}, "high_vol_exposure"),
        ({"review_weekdays": []}, "review_weekdays"),
        ({"review_weekdays": [0, 0]}, "unique"),
        ({"review_weekdays": [True]}, "integers"),
        ({"review_weekdays": [0.0]}, "integers"),
        ({"current_es_quantity": True}, "whole numbers"),
        ({"annualization_sessions": 252.0}, "whole numbers"),
        ({"rv_threshold": "NaN"}, "rv_threshold"),
    ],
)
def test_settings_payload_rejects_invalid_strategy(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        settings_payload(**overrides)


def test_html_form_converts_percentages_and_optional_positions() -> None:
    values = settings_form("csrf")
    form_items: list[tuple[str, str | UploadFile]] = []
    for name, value in values.items():
        if isinstance(value, list):
            form_items.extend((name, item) for item in value)
        else:
            form_items.append((name, str(value)))

    payload = _form_settings(FormData(form_items))

    assert payload.strategy_amount_usd == Decimal("100000.00")
    assert payload.current_es_quantity is None
    assert payload.current_mes_quantity == 4
    assert payload.rebound_fraction == Decimal("0.25")
    assert payload.rv_threshold == Decimal("0.25")
    assert payload.review_weekdays == (0, 3)


async def test_public_health_is_minimal(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "product": PRODUCT_ID,
        "version": "1.0.0",
    }
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_protected_page_stops_at_app_owned_login(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/?chart_view=risk", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?return_to=")
        login = await browser.get(response.headers["location"])
    assert login.status_code == 200
    assert "SPY Futures Allocator" in login.text
    assert "Continue with Pocket ID" in login.text
    assert "password" not in login.text.casefold()
    assert 'action="/auth/oidc/start"' in login.text
    assert "spy-futures-allocator-mark.svg" in login.text
    assert "form-action 'self' https://id.example.test" in login.headers[
        "content-security-policy"
    ]


async def test_login_card_is_centered_and_contained_on_mobile(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        login = await browser.get("/login")

    assert login.status_code == 200
    assert "*, *::before, *::after { box-sizing: border-box; }" in login.text
    assert "min-height: 100dvh" in login.text
    assert "place-items: center" in login.text
    assert "padding: 1rem" in login.text
    assert "width: min(28rem, 100%)" in login.text


async def test_passive_api_requires_session(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/api/current")
    assert response.status_code == 401


async def test_removed_event_endpoints_do_not_exist(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/api/events")
    assert response.status_code == 404


async def test_cross_site_posts_are_blocked_except_oidc_callback(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.post(
            "/auth/oidc/start",
            data={"return_to": "/"},
            headers={"Origin": "https://attacker.example"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site form submissions are forbidden"


async def test_same_origin_unauthenticated_write_reaches_auth_guard(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.post(
            "/settings",
            data={},
            headers={"Origin": "https://spy-futures-allocator.example.test"},
        )
    assert response.status_code == 401


async def test_authenticated_settings_form_calls_exact_runtime_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_value, csrf = authenticated_app(tmp_path)
    runtime = cast(ApplicationRuntime, app.state.runtime)
    saved: dict[str, object] = {}

    def save_settings(**values: object) -> object:
        saved.update(values)
        return object()

    monkeypatch.setattr(runtime, "save_settings", save_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://spy-futures-allocator.example.test",
        cookies={"spy_futures_allocator_session": session_value},
    ) as browser:
        response = await browser.post(
            "/settings",
            data=settings_form(csrf),
            headers={"Origin": "https://spy-futures-allocator.example.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?message=settings-saved"
    assert saved == {
        "strategy_amount_usd": Decimal("100000.00"),
        "current_es_quantity": None,
        "current_mes_quantity": 4,
        "ibkr_host": "gateway.internal",
        "ibkr_port": 4002,
        "ibkr_client_id": 51,
        "fast_sma_sessions": 100,
        "slow_sma_sessions": 500,
        "rebound_fraction": Decimal("0.25"),
        "rv_window_sessions": 20,
        "annualization_sessions": 252,
        "rv_threshold": Decimal("0.25"),
        "normal_exposure": Decimal("1.60"),
        "high_vol_exposure": Decimal("0.60"),
        "review_weekdays": (0, 3),
    }


async def test_invalid_settings_form_renders_422_without_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_value, csrf = authenticated_app(tmp_path)
    runtime = cast(ApplicationRuntime, app.state.runtime)
    save_calls = 0

    def save_settings(**values: object) -> object:  # noqa: ARG001
        nonlocal save_calls
        save_calls += 1
        return object()

    monkeypatch.setattr(runtime, "save_settings", save_settings)
    invalid = settings_form(csrf)
    invalid["fast_sma_sessions"] = "500"
    invalid["slow_sma_sessions"] = "100"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://spy-futures-allocator.example.test",
        cookies={"spy_futures_allocator_session": session_value},
    ) as browser:
        response = await browser.post(
            "/settings",
            data=invalid,
            headers={"Origin": "https://spy-futures-allocator.example.test"},
        )

    assert response.status_code == 422
    assert "Please correct the invalid settings values." in response.text
    assert save_calls == 0


async def test_api_rejects_boolean_integer_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_value, _ = authenticated_app(tmp_path)
    runtime = cast(ApplicationRuntime, app.state.runtime)
    save_calls = 0

    def save_settings(**values: object) -> object:  # noqa: ARG001
        nonlocal save_calls
        save_calls += 1
        return object()

    monkeypatch.setattr(runtime, "save_settings", save_settings)
    payload = settings_payload().model_dump(mode="json")
    payload["current_es_quantity"] = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://spy-futures-allocator.example.test",
        cookies={"spy_futures_allocator_session": session_value},
    ) as browser:
        response = await browser.put(
            "/api/settings",
            json=payload,
            headers={"Origin": "https://spy-futures-allocator.example.test"},
        )

    assert response.status_code == 422
    assert save_calls == 0


async def test_reset_strategy_requires_csrf_and_calls_runtime_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_value, csrf = authenticated_app(tmp_path)
    runtime = cast(ApplicationRuntime, app.state.runtime)
    reset_calls = 0

    def reset_strategy() -> object:
        nonlocal reset_calls
        reset_calls += 1
        return object()

    monkeypatch.setattr(runtime, "reset_strategy", reset_strategy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://spy-futures-allocator.example.test",
        cookies={"spy_futures_allocator_session": session_value},
    ) as browser:
        response = await browser.post(
            "/settings/reset-strategy",
            data={"csrf_token": csrf},
            headers={"Origin": "https://spy-futures-allocator.example.test"},
            follow_redirects=False,
        )
        rejected = await browser.post(
            "/settings/reset-strategy",
            data={"csrf_token": csrf[::-1]},
            headers={"Origin": "https://spy-futures-allocator.example.test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?message=strategy-reset"
    assert rejected.status_code == 403
    assert reset_calls == 1
