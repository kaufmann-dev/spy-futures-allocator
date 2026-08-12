from __future__ import annotations

import base64
from pathlib import Path

import httpx

from app.config import AppConfig
from app.main import create_app


def config(tmp_path: Path) -> AppConfig:
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    return AppConfig.model_validate(
        {
            "APP_ORIGIN": "https://arl5-simplified.example.test",
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
        base_url="https://arl5-simplified.example.test",
    )


async def test_public_health_is_minimal(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "product": "ARL5-MES-SIZING-EVENTS-1.0.0",
        "version": "1.0.0",
    }
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_protected_page_stops_at_app_owned_login(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/?view=compact", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?return_to=")
        login = await browser.get(response.headers["location"])
    assert login.status_code == 200
    assert "Continue with Pocket ID" in login.text
    assert "password" not in login.text.casefold()
    assert 'action="/auth/oidc/start"' in login.text


async def test_passive_api_requires_session(tmp_path: Path) -> None:
    async with client(tmp_path) as browser:
        response = await browser.get("/api/current")
    assert response.status_code == 401


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
            headers={"Origin": "https://arl5-simplified.example.test"},
        )
    assert response.status_code == 401
